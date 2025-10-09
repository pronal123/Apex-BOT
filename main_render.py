# ====================================================================================
# Apex BOT v17.0.5 - Custom Enhancement (TP/SL PnL, Dynamic TP, Strict Scoring)
# - NEW: 1000 USD Trade PnL notification. (User Request 1)
# - NEW: Dynamic TP based on Timeframe-specific ATR Multipliers. (User Request 2)
# - NEW: Trend Consistency Bonus (+0.10) for stricter scoring. (User Request 3)
# - FIX: analyze_single_timeframe 関数内で Pandas Series (last_row/prev_row) のキーアクセスを
#        ['key'] から .get('key', np.nan) に変更し、テクニカル指標の計算失敗による KeyError を解消。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt 
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import yfinance as yf
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 
import random 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             
TOP_SIGNAL_COUNT = 3        
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           
ATR_LENGTH = 14 # 一般的なATRの期間
SMA_LENGTH = 20 # 一般的なSMAの期間

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 

# V17.0.5 CUSTOM: New Trading Size & Dynamic TP Parameters (User Request 1 & 2)
TRADE_SIZE_USD = 1000.0             # 仮定の取引サイズ (1000ドル)
POSITION_CAPITAL = TRADE_SIZE_USD   # 既存の変数にも適用
TP_ATR_MULTIPLIERS = {              # 時間軸に応じたATR倍率 (動的TP計算用)
    '15m': 1.8, # 1.8 ATR
    '1h': 2.2,  # 2.2 ATR
    '4h': 3.0,  # 3.0 ATR
}
TREND_CONSISTENCY_BONUS = 0.10      # 複数指標のトレンド一致ボーナス (User Request 3)

# スコアリングロジック用の定数 
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               
RSI_MOMENTUM_HIGH = 60              
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """時間足に基づきTP到達目安を算出する (ログメッセージ用)"""
    if timeframe == '15m': return "数時間以内 (2〜8時間)"
    if timeframe == '1h': return "半日以内 (6〜24時間)"
    if timeframe == '4h': return "数日以内 (2〜7日)"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.4f}"
    if price >= 0.1: return f"{price:,.6f}"
    return f"{price:,.8f}"

def format_pnl_utility_telegram(pnl_usd: float) -> str:
    """損益額をTelegram表示用に整形し、色付けする"""
    if pnl_usd > 0.0001:
        return f"<ins>+${pnl_usd:,.2f}</ins> 🟢"
    elif pnl_usd < -0.0001:
        # マイナス記号を付けて、赤色にする (例: -100.00)
        return f"<ins>${pnl_usd:,.2f}</ins> 🔴" 
    return f"+$0.00 🟡"

def send_telegram_html(message: str) -> bool:
    """TelegramにHTML形式でメッセージを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        logging.info("Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        logging.error(f"Telegram HTTP Error: {e.response.text if e.response else 'N/A'}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Request Error: {e}")
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    # スコア厳格化に合わせて、勝率の傾きを調整 (v17.0.5)
    adjusted_rate = 0.50 + (score - 0.50) * 1.6 
    return max(0.40, min(0.90, adjusted_rate))

def calculate_pnl_at_pivot(target_price: float, entry: float, side_long: bool, capital: float) -> float:
    """Pivot価格到達時の損益を計算する (1x想定)"""
    if target_price <= 0 or entry <= 0: return 0.0
    
    # 数量 = 資本 / エントリー価格
    quantity = capital / entry
    
    # 損益 = 数量 * (目標価格 - エントリー価格)
    pnl = quantity * (target_price - entry)
    
    # ショートの場合は符号を反転させる (目標価格がエントリー価格より低いと利益になるため)
    if not side_long:
        pnl = -pnl
        
    return pnl

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.5 CUSTOM対応)
    """
    global POSITION_CAPITAL
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    # スコア閾値 (SIGNAL_THRESHOLD) 以上のシグナルのみを対象とする
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals:
        return "" 
        
    # 最もスコアが高いシグナルを採用
    best_signal = max(
        high_score_signals, 
        key=lambda s: (
            s.get('score', 0.5), 
            s.get('rr_ratio', 0.0), 
            s.get('tech_data', {}).get('adx', 0.0), 
            -s.get('tech_data', {}).get('atr_value', 1.0),
            s.get('symbol', '')
        )
    )
    
    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0) # 初期の追跡ストップ/損切位置
    entry_type = best_signal.get('entry_type', 'N/A') 

    # V17.0.5 CUSTOM: 動的TP価格と乗数 (User Request 2)
    dynamic_tp_price = best_signal.get('dynamic_tp_price', 0.0) # NEW
    tp_multiplier_used = best_signal.get('tp_multiplier_used', ATR_TRAIL_MULTIPLIER)
    
    # NOTE: DTSの遠い目標値 (tp1) はP&L計算には使用せず、動的TP (dynamic_tp_price) を使用します。
    tp_price_for_pnl = dynamic_tp_price 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算 (初期のストップ位置との差)
    sl_width_calculated = abs(entry_price - sl_price)
    
    # V17.0.5 CUSTOM: $1000 ポジションに基づくP&L計算 (1xレバレッジ) (User Request 1)
    is_long = (side == "ロング")
    sl_loss_usd = 0.0
    tp_gain_usd = 0.0
    sl_risk_percent = 0.0
    tp_gain_percent = 0.0

    if entry_price > 0 and sl_price > 0 and tp_price_for_pnl > 0:
        # 数量 = 資本 / エントリー価格
        quantity = POSITION_CAPITAL / entry_price
        
        # SL/TPの絶対損益額
        sl_risk_usd_abs = quantity * abs(entry_price - sl_price)
        tp_gain_usd_abs = quantity * abs(entry_price - tp_price_for_pnl) 
        
        # 損益率
        sl_risk_percent = (sl_risk_usd_abs / POSITION_CAPITAL) * 100
        tp_gain_percent = (tp_gain_usd_abs / POSITION_CAPITAL) * 100
        
        # 損失額は必ずマイナス、利益額は必ずプラス
        sl_loss_usd = -sl_risk_usd_abs
        tp_gain_usd = tp_gain_usd_abs

    # ----------------------------------------------------
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好 (VERY HIGH)" if score_raw >= 0.85 else ("高 (HIGH)" if score_raw >= 0.75 else "中 (MEDIUM)")
    
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    market_sentiment_str = ""
    macro_sentiment = best_signal.get('macro_context', {}).get('sentiment_fgi_proxy', 0.0)
    if macro_sentiment >= 0.05:
         market_sentiment_str = " (リスクオン傾向)"
    elif macro_sentiment <= -0.05:
         market_sentiment_str = " (リスクオフ傾向)"
    
    # 決済戦略の表示をDTSに変更
    exit_type_str = "DTS (動的追跡損切)" 
    
    # TP到達目安を追加
    time_to_tp = get_tp_reach_time(timeframe)

    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 🔥 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| 💰 **予想損益** | **<ins>損益比 1:{rr_ratio:.2f}</ins>** (損失: ${-sl_loss_usd:,.0f} / 利益: ${tp_gain_usd:,.0f}+) |\n"
        f"| ⏰ **決済戦略** | **{exit_type_str}** (目標RRR: 1:{rr_ratio:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | **{time_to_tp}** | (変動する可能性があります) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" 
        
    # V17.0.5 CUSTOM: 取引計画の表示を動的TPと乗数情報に合わせて変更 (User Request 2)
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic Trailing Stop & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション (**<ins>底/天井を狙う Limit 注文</ins>**) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width_calculated, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}) |\n"
        f"| 🟢 **TP 目標 ({tp_multiplier_used:.1f} ATR)** | <code>${format_price_utility(dynamic_tp_price, symbol)}</code> | **動的決済** (DTSにより利益最大化) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初期追跡ストップ**) |\n"
        f"----------------------------------\n"
    )

    # V17.0.5 CUSTOM: SL/TP 到達時のP&Lブロック (1000 USD ポジション) (User Request 1)
    pnl_block = (
        f"\n**💵 損益予測 ({POSITION_CAPITAL:,.0f} USD ポジションの場合) [1x想定]**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(sl_loss_usd)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd)}** | {tp_gain_percent:.2f}% |\n"
        f"----------------------------------\n"
    )
    
    # Pivot S/R 到達時のP&Lブロック
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    pivot_pnl_block = ""
    pivot_r1 = pivot_points.get('r1', 0.0)
    pivot_r2 = pivot_points.get('r2', 0.0)
    pivot_s1 = pivot_points.get('s1', 0.0)
    pivot_s2 = pivot_points.get('s2', 0.0)

    if pivot_r1 > 0 and entry_price > 0 and side in ["ロング", "ショート"]:
        
        # Long/Shortに応じてP&Lを計算
        pnl_r1 = calculate_pnl_at_pivot(pivot_r1, entry_price, is_long, POSITION_CAPITAL)
        pnl_r2 = calculate_pnl_at_pivot(pivot_r2, entry_price, is_long, POSITION_CAPITAL)
        pnl_s1 = calculate_pnl_at_pivot(pivot_s1, entry_price, is_long, POSITION_CAPITAL)
        pnl_s2 = calculate_pnl_at_pivot(pivot_s2, entry_price, is_long, POSITION_CAPITAL)
        
        # 損益ブロックの構築
        pivot_pnl_block = (
            f"\n**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚨 **抵抗線 R2** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🚀 **支持線 S2** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )
    
    # ----------------------------------------------------
    # 2. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    long_term_trend_4h = 'Neutral'
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data = s.get('tech_data', {})
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_term_trend_4h = tech_data.get('long_term_trend', 'Neutral')
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend_4h}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.75 else ("📈" if s_score >= 0.65 else "🟡" )
            
            penalty_status = f" (逆張りペナルティ: -{tech_data.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)" if tech_data.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により減点: -{tech_data.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"

            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ STOCHRSI 過熱感により減点: -{stoch_penalty * 100:.2f}点]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # V17.0.5 CUSTOM: トレンド整合性ボーナスの表示 (User Request 3)
                trend_consistency_bonus = tech_data.get('trend_consistency_bonus', 0.0)
                if trend_consistency_bonus > 0:
                    analysis_detail += f"   └ **トレンド整合性**: ✅ **{long_term_trend_4h} トレンド一致** (+{trend_consistency_bonus * 100:.2f}点)\n"
                else:
                    analysis_detail += f"   └ **トレンド整合性**: ❌ 不一致またはトレンドなし (一致ボーナスなし)\n"

                # Structural/Pivot Analysis
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "✅ 構造的S/R確証" if pivot_bonus > 0 else "❌ 構造確証なし"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (+{pivot_bonus * 100:.2f}点)\n" 

                # 出来高確証の表示
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ +{volume_bonus * 100:.2f}点 ボーナス追加 (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                else:
                    analysis_detail += f"   └ **出来高/流動性確証**: ❌ 確認なし (比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                
                # Funding Rate Analysis
                funding_rate_val = tech_data.get('funding_rate_value', 0.0)
                funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
                funding_rate_status = ""
                if funding_rate_bonus > 0:
                    funding_rate_status = f"✅ 優位性あり (+{funding_rate_bonus * 100:.2f}点)"
                elif funding_rate_bonus < 0:
                    funding_rate_status = f"⚠️ 過密ペナルティ適用 (-{abs(funding_rate_bonus) * 100:.2f}点)"
                else:
                    funding_rate_status = "❌ フィルター範囲外"
                analysis_detail += f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% (8h) - {funding_rate_status}\n"

                # Dominance Analysis
                dominance_trend = tech_data.get('dominance_trend', 'Neutral')
                dominance_bias_bonus_val = tech_data.get('dominance_bias_bonus_value', 0.0)
                dominance_status = ""
                if dominance_bias_bonus_val > 0:
                    dominance_status = f"✅ 優位性あり (+{dominance_bias_bonus_val * 100:.2f}点)"
                elif dominance_bias_bonus_val < 0:
                    dominance_status = f"⚠️ バイアス適用 (-{abs(dominance_bias_bonus_val) * 100:.2f}点)"
                else:
                    dominance_status = "❌ フィルター範囲外"
                analysis_detail += f"   └ **BTC D. バイアス**: {dominance_trend} - {dominance_status}\n"


    # 最終的なメッセージを結合
    message = (
        f"{header}\n"
        f"{trade_plan}\n"
        f"{pnl_block}" # TP/SL PnL summaryを挿入 (User Request 1)
        f"{pivot_pnl_block}" # Pivot PnL summary
        f"\n{analysis_detail}"
    )
    
    return message

# ====================================================================================
# CORE LOGIC
# ====================================================================================

async def initialize_exchange():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    
    # OKXをAsyncクライアントとして初期化
    EXCHANGE_CLIENT = ccxt_async.okx({
        'enableRateLimit': True,
        'rateLimit': 100, 
    })
    logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} (Async) で初期化しました。")


async def get_top_volume_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """OKXから出来高トップのUSDTペアを取得する"""
    if not EXCHANGE_CLIENT:
        await initialize_exchange()
    
    try:
        # マーケットデータをロード
        markets = await EXCHANGE_CLIENT.load_markets()
        
        # USDTペアかつ、契約（フューチャー・パーペチュアル）ではない現物をフィルタリング
        # NOTE: 先物・無期限契約の出来高を追う場合は 'swap' または 'future' タグを使用
        usdt_pairs = {
            s: m for s, m in markets.items() 
            if s.endswith('/USDT') and m.get('active', False)
        }

        # 出来高データの取得 (OKXはload_marketsで出来高を取得できないため、一旦取得を省略しデフォルトリストを使用)
        # 実際に出来高を取得するAPIは高負荷になるため、ここではデフォルトリストにフォールバックします。
        
        if len(usdt_pairs) > 0:
             logging.info(f"利用可能なUSDTペア: {len(usdt_pairs)} 種類。出来高データ取得をスキップし、デフォルトリストを使用します。")
             # デフォルトリストのうち、OKXに存在するペアに絞り込む
             filtered_symbols = [s for s in DEFAULT_SYMBOLS if s in usdt_pairs]
             
        else:
            filtered_symbols = DEFAULT_SYMBOLS

        
        # 出来高のソートは省略し、デフォルトリストのTOP Nを使用
        top_symbols = filtered_symbols[:limit]
        
        logging.info(f"監視対象シンボル: {len(top_symbols)} 種類を決定しました。")
        return [s.replace('/', '-') for s in top_symbols]
    
    except Exception as e:
        logging.error(f"出来高トップシンボル取得エラー: {e}")
        # エラー発生時は、前回成功時のリスト、またはデフォルトリストを使用
        if LAST_SUCCESSFUL_MONITOR_SYMBOLS:
            return LAST_SUCCESSFUL_MONITOR_SYMBOLS
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]]


async def fetch_ohlcv_data(symbol: str, timeframes: List[str]) -> Dict[str, pd.DataFrame]:
    """指定されたシンボルと時間足のOHLCVデータを取得する"""
    ohlcv_data = {}
    
    if not EXCHANGE_CLIENT:
        await initialize_exchange()
        
    ccxt_symbol = symbol.replace('-', '/') 
    
    for tf in timeframes:
        try:
            # 必要なデータ量 (最も長い期間)
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
            
            # CCXTのfetch_ohlcvはタイムスタンプ(ms), Open, High, Low, Close, Volumeのリストを返す
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, tf, limit=limit)
            
            # DataFrameに変換し、カラム名を設定
            df = pd.DataFrame(ohlcv, columns=['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume'])
            df['Datetime'] = pd.to_datetime(df['Datetime'], unit='ms')
            df.set_index('Datetime', inplace=True)
            ohlcv_data[tf] = df
            
            # レートリミット回避のための遅延
            await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

        except ccxt.DDoSProtection as e:
            logging.warning(f"DDoS保護発動: {symbol} {tf} の取得をスキップします。")
            continue
        except ccxt.ExchangeNotAvailable as e:
            logging.error(f"取引所利用不可: {e}")
            break
        except Exception as e:
            logging.error(f"OHLCV取得エラー {symbol} {tf}: {e}")
            continue
            
    return ohlcv_data

async def fetch_global_macro_context() -> Dict:
    """BTCドミナンス、資金調達率などのグローバルマクロコンテキストを取得する"""
    context = {'dominance_bias': 0.0, 'funding_rate': 0.0, 'sentiment_fgi_proxy': 0.0}

    # --- 資金調達率 (Funding Rate) の取得 (BTC) ---
    try:
        if EXCHANGE_CLIENT:
            # OKXのBTC/USDT無期限契約 (swap) の資金調達率を取得
            funding_rate_data = await EXCHANGE_CLIENT.fetch_funding_rate('BTC/USDT:USDT')
            context['funding_rate'] = funding_rate_data['fundingRate']
            logging.info(f"BTC資金調達率: {context['funding_rate']*100:.4f}%")
        
    except Exception as e:
        logging.warning(f"BTC資金調達率の取得に失敗: {e}")
        
    # --- BTCドミナンスの取得 (yfinance/外部APIを想定) ---
    try:
        # yfinanceのビットコインドミナンス指数 (BTC.D) プロキシを使用
        dom_df = yf.download("BTC-USD", period="7d", interval="4h", progress=False)
        if not dom_df.empty:
            # 過去3つの4h足の終値を使ってトレンドを判定
            closes = dom_df['Close'].iloc[-3:]
            if len(closes) == 3:
                # 緩やかな増加トレンド: 2本連続陽線または最後の足が大きく上昇
                if (closes.iloc[-1] > closes.iloc[-2] and closes.iloc[-2] > closes.iloc[-3]) or (closes.iloc[-1] > closes.iloc[-2] * 1.005):
                    context['dominance_bias'] = 1.0 # ドミナンス増加バイアス (アルトコインにネガティブ)
                    context['dominance_trend'] = 'Increasing'
                # 緩やかな減少トレンド: 2本連続陰線または最後の足が大きく下落
                elif (closes.iloc[-1] < closes.iloc[-2] and closes.iloc[-2] < closes.iloc[-3]) or (closes.iloc[-1] < closes.iloc[-2] * 0.995):
                    context['dominance_bias'] = -1.0 # ドミナンス減少バイアス (アルトコインにポジティブ)
                    context['dominance_trend'] = 'Decreasing'
                else:
                    context['dominance_bias'] = 0.0
                    context['dominance_trend'] = 'Neutral'
            
    except Exception as e:
        logging.warning(f"BTCドミナンスデータの取得に失敗: {e}")
        context['dominance_trend'] = 'Neutral'

    # --- センチメント指標 (FGIの代わりに、簡易的な価格変動率をプロキシとする) ---
    try:
        # BTCの24時間価格変動率をセンチメントのプロキシとする
        btc_ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv('BTC/USDT', '1d', limit=2)
        if len(btc_ohlcv) == 2:
            prev_close = btc_ohlcv[0][4]
            current_close = btc_ohlcv[1][4]
            if prev_close > 0:
                sentiment_proxy = (current_close / prev_close - 1.0) * 10.0 # 10倍して感度を上げる
                context['sentiment_fgi_proxy'] = sentiment_proxy 
    except Exception as e:
        logging.warning(f"センチメントプロキシの取得に失敗: {e}")
        
    logging.info(f"グローバルマクロコンテキスト: Dom Bias={context['dominance_bias']:.2f}, FR={context['funding_rate']*100:.4f}%, Sentiment={context['sentiment_fgi_proxy']:.2f}")
    return context


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """共通のテクニカル指標を計算し、DataFrameに追加する"""
    if df.empty: return df

    # ATR (Average True Range)
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # RSI (Relative Strength Index)
    df.ta.rsi(length=ATR_LENGTH, append=True)
        
    # MACD (Moving Average Convergence Divergence)
    df.ta.macd(append=True) # デフォルト (12, 26, 9)
        
    # ADX (Average Directional Index)
    df.ta.adx(length=ATR_LENGTH, append=True) # 14期間を使用
        
    # SMA (Simple Moving Average) - 長期トレンド判定用
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, prefix='L') # SMA_L_50

    # SMA (Simple Moving Average) - 短期 VWAPプロキシ
    df.ta.sma(length=SMA_LENGTH, append=True, prefix='S') # SMA_S_20
        
    # StochRSI (Stochastic RSI)
    df.ta.stochrsi(append=True)
    
    # CCI (Commodity Channel Index)
    df.ta.cci(append=True)
    
    # Pivot Points (日足以外でも計算は可能だが、ここでは日足で計算することを想定)
    # df.ta.pivot_points(append=True) # CCXTのデータ構造によってはエラーになるため、ここでは省略し、`analyze_single_timeframe`内で個別に対処

    return df

async def analyze_single_timeframe(symbol: str, timeframe: str, df: pd.DataFrame, funding_rate: float, dominance_bias: float) -> Dict:
    """単一の時間軸でテクニカル分析を行い、シグナルを生成する (v17.0.5 CUSTOM対応)"""

    required_len = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    
    # データ不足チェック
    if len(df) < required_len or df.isnull().values.any():
        logging.warning(f"データ不足/NaN: {symbol} {timeframe} のデータが {len(df)}/{required_len} しかなく、分析要件を満たしません。")
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "DataShortage", 'score': BASE_SCORE, 'tech_data': {}}

    # 1. テクニカル指標の計算 (calculate_indicatorsを呼ぶことで計算済みと仮定)
    df = calculate_indicators(df.copy()) # コピーを渡す

    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    
    # 2. 基本情報の抽出とインジケータ名の定義
    price = last_row.get('Close', np.nan)
    open_price = last_row.get('Open', np.nan)
    high_price = last_row.get('High', np.nan)
    low_price = last_row.get('Low', np.nan)
    
    atr_col = f'ATR_{ATR_LENGTH}'
    rsi_col = f'RSI_{ATR_LENGTH}'
    adx_col = f'ADX_{ATR_LENGTH}'
    sma_long_col = f'L_SMA_{LONG_TERM_SMA_LENGTH}'
    sma_short_col = f'S_SMA_{SMA_LENGTH}'
    macd_hist_col = 'MACDH_12_26_9'
    stochrsi_col = 'STOCHRSIk_14_14_3_3'
    cci_col = 'CCI_14_0.015' # デフォルト設定

    if np.isnan(price) or np.isnan(last_row.get(atr_col, np.nan)):
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "DataError", 'score': BASE_SCORE, 'tech_data': {}}

    atr_value = last_row.get(atr_col, 0.0)
    rsi_val = last_row.get(rsi_col, np.nan)
    macd_hist_val = last_row.get(macd_hist_col, np.nan)
    adx_value = last_row.get(adx_col, np.nan)
    sma_long_value = last_row.get(sma_long_col, np.nan)
    stochrsi_val = last_row.get(stochrsi_col, np.nan)
    
    # 3. シグナル方向と初期スコアの決定
    side = "Neutral"
    entry_type = "N/A"
    analysis_score = BASE_SCORE
    
    # RSI, MACD, StochRSI, 価格トレンドの複合的な判定ロジック
    
    # --- ロングシグナル ---
    long_conditions = (
        rsi_val < RSI_OVERSOLD and 
        (rsi_val > prev_row.get(rsi_col, rsi_val) or (rsi_val > RSI_MOMENTUM_LOW and macd_hist_val > 0)) and # RSIが底を打って反転 OR モメンタムが上向きに転換
        stochrsi_val < 20 # ストキャスティクスRSIが売られすぎ圏内
    )

    # --- ショートシグナル ---
    short_conditions = (
        rsi_val > RSI_OVERBOUGHT and 
        (rsi_val < prev_row.get(rsi_col, rsi_val) or (rsi_val < RSI_MOMENTUM_HIGH and macd_hist_val < 0)) and # RSIが天井を打って反転 OR モメンタムが下向きに転換
        stochrsi_val > 80 # ストキャスティクスRSIが買われすぎ圏内
    )

    # --- シグナル決定 ---
    if long_conditions:
        side = "ロング"
        entry_type = "Limit Entry (L)"
        analysis_score += 0.05
    elif short_conditions:
        side = "ショート"
        entry_type = "Limit Entry (S)"
        analysis_score += 0.05

    if side == "Neutral":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "Neutral", 'score': BASE_SCORE, 'tech_data': {}}

    # 4. エントリーとストップロスの計算 (ATRに基づくDTSの初期値)
    entry_price = price 
    sl_distance = atr_value * ATR_TRAIL_MULTIPLIER
    
    if side == "ロング":
        sl_price = entry_price - sl_distance
        # Structural SL / Pivot S1/S2の調整ロジックはここでは省略 (外部依存のため)
    else: # ショート
        sl_price = entry_price + sl_distance
        # Structural SL / Pivot R1/R2の調整ロジックはここでは省略
        
    # 5. V17.0.5 CUSTOM: Dynamic TP Calculation (User Request 2)
    tp_multiplier = TP_ATR_MULTIPLIERS.get(timeframe, ATR_TRAIL_MULTIPLIER) 
    tp_distance = atr_value * tp_multiplier
    
    if side == "ロング":
        dynamic_tp_price = entry_price + tp_distance
    elif side == "ショート":
        dynamic_tp_price = entry_price - tp_distance
    else:
        dynamic_tp_price = 0.0 
        
    # TP1 (DTSの遠い目標値) は、動的TPの距離より大きい値として設定（例: RRR 5.0）
    tp1_price = entry_price + sl_distance * DTS_RRR_DISPLAY if side == "ロング" else entry_price - sl_distance * DTS_RRR_DISPLAY
    
    # RRRの計算 (動的TP/SL距離に基づく)
    risk_distance = abs(entry_price - sl_price)
    reward_distance = abs(dynamic_tp_price - entry_price)
    rr_ratio = reward_distance / risk_distance if risk_distance > 0 else DTS_RRR_DISPLAY

    # 6. V17.0.5 CUSTOM: Trend Consistency Bonus (User Request 3: スコア厳格化)
    trend_consistency_bonus_value = 0.0
    
    # 主要トレンド指標の確認: MACD (モメンタム), 長期SMA (長期トレンド), ADX (トレンド強度)
    trend_consistency_aligned = False
    
    if not np.isnan(macd_hist_val) and not np.isnan(sma_long_value) and not np.isnan(adx_value):
        # 1. MACDモメンタムの一致
        macd_aligned = (side == "ロング" and macd_hist_val > 0) or (side == "ショート" and macd_hist_val < 0)
        
        # 2. 長期SMAトレンドの一致
        sma_aligned = (side == "ロング" and price > sma_long_value) or (side == "ショート" and price < sma_long_value)
        
        # 3. ADXによるトレンドの存在
        adx_sufficient = adx_value >= 25 # 厳しめに25以上

        if macd_aligned and sma_aligned and adx_sufficient:
            trend_consistency_aligned = True

    if trend_consistency_aligned:
        trend_consistency_bonus_value = TREND_CONSISTENCY_BONUS
        analysis_score += trend_consistency_bonus_value
        
    # 7. その他のスコアリングロジック (既存ロジックを反映)
    
    # 長期トレンドとの逆張りペナルティ
    long_term_trend = "Uptrend" if price > sma_long_value else "Downtrend"
    is_reversal = (side == "ロング" and long_term_trend == "Downtrend") or (side == "ショート" and long_term_trend == "Uptrend")
    long_term_reversal_penalty_value = 0.0
    if is_reversal:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        analysis_score -= long_term_reversal_penalty_value
        
    # MACDクロスによるペナルティ (シグナル方向とMACDの向きが一致しない場合)
    macd_cross_valid = (side == "ロング" and macd_hist_val > 0) or (side == "ショート" and macd_hist_val < 0)
    macd_cross_penalty_value = 0.0
    if not macd_cross_valid and abs(macd_hist_val) > 0.0001:
        macd_cross_penalty_value = MACD_CROSS_PENALTY
        analysis_score -= macd_cross_penalty_value

    # StochRSIの過熱感ペナルティ
    stoch_filter_penalty = 0.0
    if side == "ロング" and stochrsi_val > 50:
        stoch_filter_penalty = 0.05
        analysis_score -= stoch_filter_penalty
    elif side == "ショート" and stochrsi_val < 50:
        stoch_filter_penalty = 0.05
        analysis_score -= stoch_filter_penalty
        
    # 資金調達率によるボーナス/ペナルティ
    funding_rate_bonus_value = 0.0
    if side == "ロング" and funding_rate < -FUNDING_RATE_THRESHOLD: # FRがマイナスでロング (逆張りの優位性)
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
        analysis_score += funding_rate_bonus_value
    elif side == "ショート" and funding_rate > FUNDING_RATE_THRESHOLD: # FRがプラスでショート
        funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
        analysis_score += funding_rate_bonus_value
    elif side == "ロング" and funding_rate > FUNDING_RATE_THRESHOLD * 2: # FRが過度にプラスでロング (過密ペナルティ)
        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        analysis_score += funding_rate_bonus_value
    elif side == "ショート" and funding_rate < -FUNDING_RATE_THRESHOLD * 2: # FRが過度にマイナスでショート
        funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
        analysis_score += funding_rate_bonus_value

    # BTCドミナンスによるボーナス/ペナルティ (アルトコインの場合のみ)
    dominance_bias_bonus_value = 0.0
    if symbol != 'BTC-USDT' and abs(dominance_bias) > 0.01:
        if side == "ロング" and dominance_bias < 0: # アルトロングでドミナンス減少 (ポジティブ)
            dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
            analysis_score += dominance_bias_bonus_value
        elif side == "ショート" and dominance_bias > 0: # アルトショートでドミナンス増加 (ポジティブ)
            dominance_bias_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY
            analysis_score += dominance_bias_bonus_value
        elif side == "ロング" and dominance_bias > 0: # アルトロングでドミナンス増加 (ネガティブ)
            dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
            analysis_score += dominance_bias_bonus_value
        elif side == "ショート" and dominance_bias < 0: # アルトショートでドミナンス減少 (ネガティブ)
            dominance_bias_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
            analysis_score += dominance_bias_bonus_value

    # スコアの範囲を制限
    analysis_score = max(0.01, min(1.0, analysis_score))
    
    # Pivot Pointsの計算 (日足で計算すると仮定)
    pivot_points = {}
    if timeframe == '4h' or timeframe == '1h':
        # 簡略化のため、ここでは計算を省略し、デフォルト値を使用
        # 実際の運用では、日足データから別途計算するか、ta.pivot_pointsを使用します。
        pivot_points = {'pp': price, 'r1': price + atr_value*4, 'r2': price + atr_value*7, 's1': price - atr_value*4, 's2': price - atr_value*7}
    
    # 8. 最終的な結果の構築
    signal_result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'price': price,
        'side': side,
        'entry_type': entry_type,
        'entry': entry_price,
        'sl': sl_price,
        'tp1': tp1_price, 
        'dynamic_tp_price': dynamic_tp_price, # NEW
        'tp_multiplier_used': tp_multiplier, # NEW
        'rr_ratio': rr_ratio,
        'score': analysis_score,
        'regime': "Trend" if adx_value >= ADX_TREND_THRESHOLD else "Ranging", 
        'macro_context': GLOBAL_MACRO_CONTEXT,
        'tech_data': {
            'atr_value': atr_value,
            'adx': adx_value,
            'rsi': rsi_val,
            'macd_hist': macd_hist_val,
            'cci': last_row.get(cci_col, np.nan),
            'long_term_trend': long_term_trend,
            'long_term_reversal_penalty': is_reversal,
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
            'trend_consistency_bonus': trend_consistency_bonus_value, # NEW
            'macd_cross_valid': macd_cross_valid,
            'macd_cross_penalty_value': macd_cross_penalty_value,
            'stoch_filter_penalty': stoch_filter_penalty,
            'structural_sl_used': False, # ロジックを実装していないためFalse
            'structural_pivot_bonus': 0.0, # ロジックを実装していないため0.0
            'pivot_points': pivot_points,
            'volume_confirmation_bonus': 0.0, # ロジックを実装していないため0.0
            'volume_ratio': 1.0, # ロジックを実装していないため1.0
            'funding_rate_value': funding_rate,
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'dominance_trend': GLOBAL_MACRO_CONTEXT.get('dominance_trend', 'Neutral'),
            'dominance_bias_bonus_value': dominance_bias_bonus_value,
            'vwap_consistent': abs(price - last_row.get(sma_short_col, price)) < atr_value * 0.5, # 簡易的なVWAP一致
        }
    }
    return signal_result

async def run_technical_analysis(symbol: str) -> List[Dict]:
    """全時間軸で分析を実行し、結果を統合する"""
    timeframes = list(REQUIRED_OHLCV_LIMITS.keys())
    ohlcv_data = await fetch_ohlcv_data(symbol, timeframes)
    
    if not ohlcv_data:
        return [{'symbol': symbol, 'timeframe': 'N/A', 'side': "ExchangeError", 'score': BASE_SCORE, 'tech_data': {}}]
    
    # グローバルコンテキストからマクロ変数を取得
    funding_rate = GLOBAL_MACRO_CONTEXT.get('funding_rate', 0.0)
    dominance_bias = GLOBAL_MACRO_CONTEXT.get('dominance_bias', 0.0)
    
    tasks = []
    for tf, df in ohlcv_data.items():
        if not df.empty and len(df) >= REQUIRED_OHLCV_LIMITS.get(tf, 500):
            tasks.append(analyze_single_timeframe(symbol, tf, df, funding_rate, dominance_bias))
        else:
            logging.warning(f"分析スキップ: {symbol} {tf} のデータが不足しています。")

    if not tasks:
        return [{'symbol': symbol, 'timeframe': 'N/A', 'side': "DataShortage", 'score': BASE_SCORE, 'tech_data': {}}]
        
    analysis_results = await asyncio.gather(*tasks)
    
    # 各時間軸の結果をフィルタリング
    valid_results = [res for res in analysis_results if res['side'] not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    # 4hの結果に長期トレンド情報が含まれていることを確認
    for res in valid_results:
        if res['timeframe'] == '4h':
            for other_res in valid_results:
                 # すべてのシグナルに4hの長期トレンド情報を含める (メッセージの表示で使用するため)
                 other_res['tech_data']['long_term_trend_4h'] = res['tech_data'].get('long_term_trend', 'Neutral')
                 break
            break

    return valid_results


def integrate_analysis(analysis_list: List[List[Dict]]) -> List[Dict]:
    """複数のシンボルの分析結果を統合し、ランキングを決定する"""
    integrated_signals = []
    
    for symbol_signals in analysis_list:
        if not symbol_signals:
            continue
        
        # 1. 最もスコアが高い時間足のシグナルを採用
        best_signal = max(symbol_signals, key=lambda s: s.get('score', 0.0))
        
        if best_signal['score'] >= SIGNAL_THRESHOLD:
            # 2. 全時間足のシグナル情報を統合
            integrated_signal = best_signal.copy()
            integrated_signal['all_timeframe_signals'] = symbol_signals
            integrated_signals.append(integrated_signal)
            
    # 3. 最終スコアに基づきランキング
    # RRRとスコアを複合的に考慮してソート
    integrated_signals.sort(key=lambda s: (s['score'] * 100 + s['rr_ratio'] * 5), reverse=True)
    
    return integrated_signals


async def main_loop():
    """BOTのメインループ処理"""
    global CURRENT_MONITOR_SYMBOLS, TRADE_NOTIFIED_SYMBOLS, LAST_SUCCESS_TIME, LAST_SUCCESSFUL_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT
    
    await initialize_exchange()
    
    while True:
        try:
            current_time = time.time()
            logging.info("====================================")
            logging.info(f"🔄 Apex BOT v17.0.5 実行開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST")
            
            # 1. 監視対象シンボルの更新 (約1時間に1回)
            if current_time - LAST_SUCCESS_TIME > 3600 or not CURRENT_MONITOR_SYMBOLS:
                CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols()
                LAST_SUCCESSFUL_MONITOR_SYMBOLS = CURRENT_MONITOR_SYMBOLS.copy()
            
            # 2. グローバルマクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await fetch_global_macro_context()

            # 3. 全てのシンボルに対して分析を実行
            analysis_tasks = [run_technical_analysis(symbol) for symbol in CURRENT_MONITOR_SYMBOLS]
            all_analysis_results = await asyncio.gather(*analysis_tasks)
            
            # 4. 結果を統合し、ランキングを作成
            all_signals = [item for sublist in all_analysis_results for item in sublist]
            integrated_signals = integrate_analysis(all_analysis_results)
            LAST_ANALYSIS_SIGNALS = integrated_signals # API/ステータス更新用

            # 5. シグナルを通知
            notified_count = 0
            for rank, signal in enumerate(integrated_signals[:TOP_SIGNAL_COUNT], 1):
                symbol_key = signal['symbol']
                signal_time = current_time
                
                # クールダウンチェック
                last_notify_time = TRADE_NOTIFIED_SYMBOLS.get(symbol_key, 0)
                if signal_time - last_notify_time < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"通知スキップ: {symbol_key} はクールダウン期間中です。")
                    continue
                
                # メッセージの整形と送信
                message = format_integrated_analysis_message(symbol_key, signal['all_timeframe_signals'], rank)
                
                if message:
                    if send_telegram_html(message):
                        TRADE_NOTIFIED_SYMBOLS[symbol_key] = signal_time
                        notified_count += 1
            
            logging.info(f"✅ 処理完了。新規通知: {notified_count} 件。")
            LAST_SUCCESS_TIME = current_time
            
            # 6. クールダウン時間待機
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name} - {e}")
            
            # 例外がCCXT関連でレート制限の可能性があれば、より長く待機
            if isinstance(e, (ccxt.RateLimitExceeded, ccxt.RequestTimeout)):
                logging.warning("レート制限またはタイムアウトの可能性あり。600秒間待機します。")
                await asyncio.sleep(600)
            else:
                 await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.5 - Custom Enhancement") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.5 Startup initializing...") # バージョン更新
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v17.0.5 - Custom Enhancement", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.5 - Custom Enhancement)"})

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8000)
