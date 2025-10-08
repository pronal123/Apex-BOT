# ====================================================================================
# Apex BOT v17.0.4 - Fix Fatal KeyError in analyze_single_timeframe
# - FIX: analyze_single_timeframe 関数内で Pandas Series (last_row/prev_row) のキーアクセスを
#        ['key'] から .get('key', np.nan) に変更し、テクニカル指標の計算失敗による KeyError を解消。
# - FIX: format_integrated_analysis_message 関数内で 'regime' のアクセス方法を修正。
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
SIGNAL_THRESHOLD = 0.75             # 閾値を 0.75 に設定
TOP_SIGNAL_COUNT = 3        
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               
POSITION_CAPITAL = 1000.0           # 1x想定のポジションサイズ (USD) - NEW

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 # BTCドミナンスの偏りによる最大ボーナス/ペナルティ点

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
    """損益額をTelegram表示用に整形し、色付けする - NEW"""
    if pnl_usd > 0.0001:
        return f"<ins>+${pnl_usd:,.2f}</ins> 🟢"
    elif pnl_usd < -0.0001:
        # マイナス記号を付けて、赤色にする
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
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))

def calculate_pnl_at_pivot(target_price: float, entry: float, side_long: bool, capital: float) -> float:
    """Pivot価格到達時の損益を計算する (1x想定) - NEW Utility"""
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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.4対応)
    """
    global POSITION_CAPITAL
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
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
    tp_price = best_signal.get('tp1', 0.0) # DTS採用のため、これはあくまで遠い目標値
    sl_price = best_signal.get('sl', 0.0) # 初期の追跡ストップ/損切位置
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算 (初期のストップ位置との差)
    sl_width_calculated = abs(entry_price - sl_price)
    
    # NEW: $1000 ポジションに基づくP&L計算 (1xレバレッジ)
    is_long = (side == "ロング")
    if entry_price > 0:
        # 数量 = 資本 / エントリー価格
        quantity = POSITION_CAPITAL / entry_price
        
        # SL/TPの絶対損益額
        sl_risk_usd_abs = quantity * sl_width_calculated 
        tp_gain_usd_abs = quantity * abs(entry_price - tp_price) 
        
        # 損益率
        sl_risk_percent = (sl_risk_usd_abs / POSITION_CAPITAL) * 100
        tp_gain_percent = (tp_gain_usd_abs / POSITION_CAPITAL) * 100
        
    else:
        sl_risk_usd_abs = 0.0
        tp_gain_usd_abs = 0.0
        sl_risk_percent = 0.0
        tp_gain_percent = 0.0

    sl_loss_usd = sl_risk_usd_abs
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
        f"| 💰 **予想損益** | **<ins>損益比 1:{rr_ratio:.2f}</ins>** (損失: ${sl_loss_usd:,.0f} / 利益: ${tp_gain_usd:,.0f}+) |\n"
        f"| ⏰ **決済戦略** | **{exit_type_str}** (目標RRR: 1:{rr_ratio:.2f}+) |\n" 
        f"| ⏳ **TP到達目安** | **{time_to_tp}** | (変動する可能性があります) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot) + **0.5 ATR バッファ**" # FIX反映
        
    # 取引計画の表示をDTSに合わせて変更
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic Trailing Stop & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション (**<ins>底/天井を狙う Limit 注文</ins>**) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width_calculated, symbol)} | **初動リスク** (ATR x {ATR_TRAIL_MULTIPLIER:.1f}) |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | **動的決済** (DTSにより利益最大化) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初期追跡ストップ**) |\n"
        f"----------------------------------\n"
    )

    # NEW: SL/TP 到達時のP&Lブロック (1000 USD ポジション)
    pnl_block = (
        f"\n**📈 損益結果 ({POSITION_CAPITAL:,.0f} USD ポジションの場合)**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(-sl_risk_usd_abs)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd_abs)}** | {tp_gain_percent:.2f}% |\n"
        f"----------------------------------\n"
    )
    
    # NEW: Pivot S/R 到達時のP&Lブロック (1000 USD ポジション)
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
    
    # NEW: 構造的S/R候補ブロック (P&Lブロックで全て表示されるため、このブロックは省略される可能性あり)
    sr_info = ""
    if pivot_points and not pivot_pnl_block: # P&Lブロックがない場合にのみS/R候補を表示
        r1 = format_price_utility(pivot_r1, symbol)
        r2 = format_price_utility(pivot_r2, symbol)
        s1 = format_price_utility(pivot_s1, symbol)
        s2 = format_price_utility(pivot_s2, symbol)
        pp = format_price_utility(pivot_points.get('pp', 0.0), symbol)
        
        sr_info = (
            f"\n**🧱 構造的S/R候補 (日足)**\n"
            f"----------------------------------\n"
            f"| 候補 | 価格 (USD) | 種類 |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 🛡️ S2 / S1 | <code>${s2}</code> / <code>${s1}</code> | 主要な**支持 (Support)** 候補 |\n"
            f"| 🟡 PP | <code>${pp}</code> | ピボットポイント |\n"
            f"| ⚔️ R1 / R2 | <code>${r1}</code> / <code>${r2}</code> | 主要な**抵抗 (Resistance)** 候補 |\n"
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
            
            # NEW: 長期トレンド逆行ペナルティのハイライト
            penalty_status = ""
            if tech_data.get('long_term_reversal_penalty'):
                 penalty_status = f" (<ins>**逆張りペナルティ**</ins>: -{tech_data.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)"
            
            # NEW: MACD反転ペナルティのハイライト
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]"
            if not momentum_valid:
                 momentum_text = f"[❌ **モメンタム反転により減点** : -{tech_data.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]"
            if not vwap_consistent:
                 vwap_text = "[⚠️ VWAP不一致: NG]"

            # NEW: StochRSIペナルティのハイライト
            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ **STOCHRSI 過熱感により減点** : -{stoch_penalty * 100:.2f}点]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                # FIX: regimeをtech_dataから取得
                regime = best_signal.get('tech_data', {}).get('regime', 'N/A')
                
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # NEW: 構造的S/Rボーナスのハイライト
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "❌ 構造確証なし"
                if pivot_bonus > 0:
                     pivot_status = f"✅ **構造的S/R確証**"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (<ins>**+{pivot_bonus * 100:.2f}点 ボーナス追加**</ins>)\n"

                # NEW: 出来高確証ボーナスのハイライト
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ <ins>**+{volume_bonus * 100:.2f}点 ボーナス追加**</ins> (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                else:
                    analysis_detail += f"   └ **出来高/流動性確証**: ❌ 確認なし (比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
           
                # NEW: Funding Rateボーナス/ペナルティのハイライト
                funding_rate_val = tech_data.get('funding_rate_value', 0.0)
                funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
                funding_rate_status = ""
                if funding_rate_bonus > 0:
                    funding_rate_status = f"✅ **優位性あり** (<ins>**+{funding_rate_bonus * 100:.2f}点**</ins>)"
                elif funding_rate_bonus < 0:
                    funding_rate_status = f"⚠️ **過密ペナルティ適用** (<ins>**-{abs(funding_rate_bonus) * 100:.2f}点**</ins>)"
                else:
                    funding_rate_status = "❌ フィルター範囲外"
                
                analysis_detail += f"   └ **資金調達率 (FR)**: {funding_rate_val * 100:.4f}% (8h) - {funding_rate_status}\n"

                # NEW: Dominance Biasボーナス/ペナルティのハイライト
                dominance_trend = tech_data.get('dominance_trend', 'Neutral')
                dominance_bonus = tech_data.get('dominance_bias_bonus_value', 0.0)
                
                dominance_status = ""
                if dominance_bonus > 0:
                    dominance_status = f"✅ **優位性あり** (<ins>**+{dominance_bonus * 100:.2f}点**</ins>)"
                elif dominance_bonus < 0:
                    dominance_status = f"⚠️ **バイアスにより減点適用** (<ins>**-{abs(dominance_bonus) * 100:.2f}点**</ins>)"
                else:
                    dominance_status = "❌ フィルター範囲外/非該当"
                
                trend_display = ""
                if symbol != 'BTC-USDT':
                     trend_display = f" (Altcoin Bias: {dominance_trend})"
                
                analysis_detail += f"   └ **BTCドミナンス**: {dominance_trend} トレンド{trend_display} - {dominance_status}\n"


    # 3. リスク管理とフッター
    # FIX: regimeをtech_dataから取得
    regime = best_signal.get('tech_data', {}).get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v17.0.4** - KeyError Fix |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ Limit注文は、価格が指定水準に到達した際のみ約定します。DTS戦略では、価格が有利な方向に動いた場合、SLが自動的に追跡され利益を最大化します。</pre>"
    )

    return header + trade_plan + pnl_block + pivot_pnl_block + sr_info + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    EXCHANGE_CLIENT = ccxt_async.okx({
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'} 
    })
    
    if EXCHANGE_CLIENT:
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - リアル接続, Default: Swap)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


def convert_symbol_to_okx_swap(symbol: str) -> str:
    """USDT現物シンボル (BTC/USDT) をOKXの無期限スワップシンボル (BTC-USDT) に変換する"""
    return symbol.replace('/', '-')

async def fetch_funding_rate(symbol: str) -> float:
    """OKXからシンボルの直近の資金調達率を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
    try:
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol)
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        return 0.0

async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }

        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
        else:
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning("⚠️ 出来高データを取得できませんでした。前回成功したリストを使用します。")

    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
        logging.warning("⚠️ 出来高データ取得エラー。前回成功したリストにフォールバックします。")

        
async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """CCXTを使用してOHLCVデータを取得し、エラー発生時にフォールバックする"""
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT:
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        if 'market symbol' in str(e) or 'not found' in str(e):
             spot_symbol = symbol.replace('-', '/')
             try:
                 ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
                 if not ohlcv or len(ohlcv) < 30: 
                     return [], "DataShortage", client_name
                 return ohlcv, "Success", client_name
             except Exception:
                 return [], "ExchangeError", client_name
        
        return [], "ExchangeError", client_name
        
    except Exception as e:
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """
    マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias)
    """
    
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC-USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH-USDT", '4h')

    btc_trend = 0
    eth_trend = 0
    btc_change = 0.0
    eth_change = 0.0
    
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()

    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH)
        df_btc.dropna(subset=['sma'], inplace=True)
        
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]:
                btc_trend = 1 # Long
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]:
                btc_trend = -1 # Short
        
            if len(df_btc) >= 2:
                btc_change = (df_btc['close'].iloc[-1] - df_btc['close'].iloc[-2]) / df_btc['close'].iloc[-2]

    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]:
                eth_trend = 1 # Long
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]:
                eth_trend = -1 # Short

            if len(df_eth) >= 2:
                eth_change = (df_eth['close'].iloc[-1] - df_eth['close'].iloc[-2]) / df_eth['close'].iloc[-2]

    # 2. FGI Proxyの計算 (恐怖指数/市場センチメント)
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = 0.07 # 強いリスクオン (ボーナス強化)
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -0.07 # 強いリスクオフ (恐怖) (ボーナス強化)
        
    # 3. BTC Dominance Proxyの計算
    dominance_trend = "Neutral"
    dominance_bias_score = 0.0
    DOM_DIFF_THRESHOLD = 0.002 # 0.2%以上の差でドミナンスに偏りありと判断
    
    if status_btc == "Success" and status_eth == "Success" and len(df_btc) >= 2 and len(df_eth) >= 2:
        # BTCとETHの直近の価格変化率の差を計算 (BTCドミナンスの変化の代理変数)
        dom_diff = btc_change - eth_change
        
        if dom_diff > DOM_DIFF_THRESHOLD:
            # BTCの上昇率がETHより高い -> BTC dominanceの上昇トレンド
            dominance_trend = "Increasing"
            dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY # Altcoinにペナルティ
        elif dom_diff < -DOM_DIFF_THRESHOLD:
            # ETHの上昇率がBTCより高い -> BTC dominanceの下降トレンド
            dominance_trend = "Decreasing"
            dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoinにボーナス
        else:
            dominance_trend = "Neutral"
            dominance_bias_score = 0.0

    macro_context = {
        "btc_trend": btc_trend,
        "eth_trend": eth_trend,
        "sentiment_fgi_proxy": sentiment_score,
        "dominance_trend": dominance_trend,
        "dominance_bias_score": dominance_bias_score
    }
    logging.info(f"✅ マクロコンテキスト更新: BTC Trend: {btc_trend}, FGI Proxy: {sentiment_score:.2f}, Dominance Bias: {dominance_bias_score:.2f} ({dominance_trend})")
    return macro_context

# ====================================================================================
# TRADING STRATEGY & SCORING LOGIC
# ====================================================================================

def calculate_pivot_points(df: pd.DataFrame) -> Dict[str, float]:
    """ Pivot Point (PP) と主要なサポート/レジスタンス (S/R) を計算する Daily Pivot (Classic) を使用 """
    # 前日のローソク足の情報を取得
    if len(df) < 2: return {}
    
    prev_day = df.iloc[-2]
    high = prev_day['high']
    low = prev_day['low']
    close = prev_day['close']

    # Pivot Point (PP)
    pp = (high + low + close) / 3

    # Resistance Levels
    r1 = (2 * pp) - low
    r2 = pp + (high - low)
    r3 = r2 + (high - low)

    # Support Levels
    s1 = (2 * pp) - high
    s2 = pp - (high - low)
    s3 = s2 - (high - low)

    return {
        'pp': pp,
        'r1': r1,
        'r2': r2,
        'r3': r3,
        's1': s1,
        's2': s2,
        's3': s3
    }


def analyze_single_timeframe(symbol: str, timeframe: str, ohlcv: List[List[float]], macro_context: Dict) -> Dict:
    """単一の時間足に基づいた詳細なテクニカル分析とシグナルスコアリングを実行する"""
    if not ohlcv: return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}

    # 1. データフレーム構築とテクニカル指標の計算
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')

    # NaNを含む行を除外 (特に ta.sma で発生する可能性)
    df.dropna(subset=['close', 'high', 'low', 'volume'], inplace=True)
    if len(df) < 30: return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}

    # インジケータ計算 (pandas_ta 使用)
    df.ta.adx(length=14, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.cci(length=20, append=True)
    df.ta.stoch(k=14, d=3, append=True)
    df.ta.atr(length=14, append=True)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)

    # 2. 最新のデータとスコアの初期化
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2] if len(df) >= 2 else last_row
    
    current_price = last_row.get('close', np.nan) # close, high, low, volumeはohlcvの基本データなので通常は存在するが、念のため.get()
    
    # FIX: KeyError対策のため、直接アクセス (['key']) を .get('key', np.nan) に変更
    current_rsi = last_row.get('RSI_14', np.nan)
    current_macd_hist = last_row.get('MACDh_12_26_9', np.nan)
    current_adx = last_row.get('ADX_14', np.nan)
    current_atr = last_row.get('ATR_14', np.nan)
    current_sma = last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan)
    current_stoch_k = last_row.get('STOCHk_14_3_3', np.nan)
    current_stoch_d = last_row.get('STOCHd_14_3_3', np.nan)
    
    bb_lower = last_row.get('BBL_20_2.0', np.nan) 
    bb_upper = last_row.get('BBU_20_2.0', np.nan)
    bb_mid = last_row.get('BBM_20_2.0', np.nan)
    
    # FIX: KeyError対策のため、直接アクセス (['key']) を .get('key', np.nan) に変更
    prev_rsi = prev_row.get('RSI_14', np.nan)
    prev_macd_hist = prev_row.get('MACDh_12_26_9', np.nan)
    prev_stoch_k = prev_row.get('STOCHk_14_3_3', np.nan)
    prev_stoch_d = prev_row.get('STOCHd_14_3_3', np.nan)

    base_score = BASE_SCORE # 0.40
    side = 'Neutral'
    score_modifiers = {}
    
    # 3. Pivot Pointの計算 (構造的S/Rライン)
    # 4h足のみで計算し、他の足は参照する（ここでは計算のみ）
    pivot_points = calculate_pivot_points(df)

    # 4. 長期トレンドフィルター (4h足のみで適用)
    long_term_trend = 'Neutral'
    if timeframe == '4h':
        # 50 SMA を長期トレンドの指標とする
        if not np.isnan(current_sma):
            if current_price > current_sma:
                long_term_trend = 'Long'
            elif current_price < current_sma:
                long_term_trend = 'Short'
        
        # 4h足はトレンド判定のみを行い、スコアはリターンしない
        if long_term_trend != 'Neutral':
             base_score = 1.0 # 4h足のトレンド方向が確定したら1.0を返す
        else:
             base_score = 0.5 # トレンドが中立の場合は0.5

        return {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': long_term_trend,
            'score': base_score,
            'tech_data': {
                'long_term_trend': long_term_trend,
                'price': current_price
            }
        }
    
    # 5. シグナル方向の決定とベーススコアの加算（4h足以外）
    
    # NaNチェックを導入 (計算に失敗した場合はシグナル生成不可)
    if np.isnan(current_macd_hist) or np.isnan(prev_macd_hist) or np.isnan(current_rsi) or np.isnan(prev_rsi) or np.isnan(current_atr):
         return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.0}

    # MACDヒストグラムのクロスオーバー (最も強いシグナル)
    if current_macd_hist > 0 and prev_macd_hist <= 0:
        side = 'Long'
        base_score += 0.30
        score_modifiers['macd_cross'] = 0.30
    elif current_macd_hist < 0 and prev_macd_hist >= 0:
        side = 'Short'
        base_score += 0.30
        score_modifiers['macd_cross'] = 0.30
        
    # RSIのモメンタム加速
    if side == 'Long' and current_rsi > RSI_MOMENTUM_LOW:
        base_score += 0.10
        score_modifiers['rsi_momentum'] = 0.10
    elif side == 'Short' and current_rsi < RSI_MOMENTUM_HIGH:
        base_score += 0.10
        score_modifiers['rsi_momentum'] = 0.10

    # 6. ペナルティの適用 (フィルタリング)
    
    # A. MACD反転ペナルティ (v17.0.1 修正ロジック)
    macd_cross_valid = True
    macd_cross_penalty_value = 0.0
    if side == 'Long' and current_macd_hist < 0 and prev_macd_hist > 0:
        # Longシグナルなのに、MACDヒストグラムが直近で陰転した
        base_score -= MACD_CROSS_PENALTY
        macd_cross_valid = False
        macd_cross_penalty_value = MACD_CROSS_PENALTY
    elif side == 'Short' and current_macd_hist > 0 and prev_macd_hist < 0:
        # Shortシグナルなのに、MACDヒストグラムが直近で陽転した
        base_score -= MACD_CROSS_PENALTY
        macd_cross_valid = False
        macd_cross_penalty_value = MACD_CROSS_PENALTY
    
    # B. 長期トレンド逆行ペナルティ (4hトレンドの参照)
    long_term_reversal_penalty = False
    long_term_reversal_penalty_value = 0.0
    macro_btc_trend = macro_context.get('btc_trend', 0)
    
    if (side == 'Long' and macro_btc_trend == -1) or \
       (side == 'Short' and macro_btc_trend == 1):
        # 逆張りペナルティ適用
        base_score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty = True
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        
    # C. ボラティリティペナルティ (BB外でのエントリー回避)
    volatility_penalty = 0.0
    if not np.isnan(bb_lower) and not np.isnan(bb_upper):
        if (side == 'Long' and current_price < bb_lower) or \
           (side == 'Short' and current_price > bb_upper):
            # 価格がBBの外側にある場合はペナルティ
            base_score -= VOLATILITY_BB_PENALTY_THRESHOLD * 0.01 # 5.0%ペナルティ
            volatility_penalty = VOLATILITY_BB_PENALTY_THRESHOLD * 0.01

    # D. StochRSI 過熱感ペナルティ
    stoch_filter_penalty = 0.0
    if not np.isnan(current_stoch_k) and not np.isnan(current_stoch_d):
        if side == 'Long' and (current_stoch_k > 85 or current_stoch_d > 85):
            base_score -= 0.10
            stoch_filter_penalty = 0.10
        elif side == 'Short' and (current_stoch_k < 15 or current_stoch_d < 15):
            base_score -= 0.10
            stoch_filter_penalty = 0.10
            
    # E. 資金調達率 (Funding Rate) バイアスフィルター (v17.0.1 修正ロジック)
    funding_rate_val = 0.0
    funding_rate_bonus_value = 0.0
    if timeframe == '1h':
        # 1h足でFRを取得し、スコアに加減点
        funding_rate_val = macro_context.get('funding_rate', 0.0)
        
        if funding_rate_val > FUNDING_RATE_THRESHOLD: # FRが高すぎる（ロング過密）
            if side == 'Long':
                # ロングなのにロング過密 -> ペナルティ
                base_score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
            elif side == 'Short':
                # ショートでロング過密 -> ボーナス
                base_score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
        elif funding_rate_val < -FUNDING_RATE_THRESHOLD: # FRが低すぎる（ショート過密）
            if side == 'Short':
                # ショートなのにショート過密 -> ペナルティ
                base_score -= FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY
            elif side == 'Long':
                # ロングでショート過密 -> ボーナス
                base_score += FUNDING_RATE_BONUS_PENALTY
                funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY

    # F. BTC Dominance バイアスフィルター
    dominance_bonus_value = 0.0
    if symbol != 'BTC-USDT' and timeframe == '1h':
        dominance_trend = macro_context.get('dominance_trend', 'Neutral')
        dominance_bias_score = macro_context.get('dominance_bias_score', 0.0)
        
        # Dominance Increasing: Altcoin Short/Neutralでボーナス, Longでペナルティ
        if dominance_trend == 'Increasing':
            if side == 'Long':
                base_score -= DOMINANCE_BIAS_BONUS_PENALTY
                dominance_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
        # Dominance Decreasing: Altcoin Longでボーナス, Short/Neutralでペナルティ
        elif dominance_trend == 'Decreasing':
            if side == 'Short':
                base_score -= DOMINANCE_BIAS_BONUS_PENALTY
                dominance_bonus_value = -DOMINANCE_BIAS_BONUS_PENALTY
            elif side == 'Long':
                base_score += DOMINANCE_BIAS_BONUS_PENALTY
                dominance_bonus_value = DOMINANCE_BIAS_BONUS_PENALTY

    # 7. 構造的S/Rライン (Pivot Point) ボーナス (v17.0.1 修正ロジック)
    structural_pivot_bonus = 0.0
    structural_sl_used = False
    
    if side == 'Long' and pivot_points.get('s1') and current_price <= pivot_points['s1'] * 1.002: # S1の0.2%以内
        base_score += 0.07 # 構造的根拠のボーナス
        structural_pivot_bonus = 0.07
    elif side == 'Short' and pivot_points.get('r1') and current_price >= pivot_points['r1'] * 0.998: # R1の0.2%以内
        base_score += 0.07
        structural_pivot_bonus = 0.07

    # 8. 出来高確証ボーナス
    volume_confirmation_bonus = 0.0
    volume_ratio = 0.0
    if len(df) >= 30:
        volume_avg = df['volume'].iloc[-30:-1].mean()
        if not np.isnan(volume_avg) and volume_avg > 0:
            volume_ratio = last_row.get('volume', 0.0) / volume_avg
            if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER: # 2.5倍以上
                base_score += 0.12 # 出来高ボーナス
                volume_confirmation_bonus = 0.12

    # 9. 最終スコアの調整とシグナル決定
    final_score = max(0.01, min(1.0, base_score))
    
    # 10. DTS (Dynamic Trailing Stop) ベースのSL/TPの計算
    entry_price = current_price
    if side == 'Long':
        # S1が近い場合はLimit Entryを採用し、SLをS1外側に設定
        if structural_pivot_bonus > 0 and pivot_points.get('s1'):
            entry_price = pivot_points['s1'] # Limit EntryをS1に設定
            # SLはS1よりもATRバッファだけ外側（下）に設定
            sl_price = entry_price - (current_atr * 0.5) 
            entry_type = 'Limit'
            structural_sl_used = True
        else:
            # Market Entry
            sl_price = current_price - (current_atr * ATR_TRAIL_MULTIPLIER)
            entry_type = 'Market'
            
        # TP目標はRRR 5.0 で計算 (DTSの最低目標として表示)
        sl_width_calculated = entry_price - sl_price
        tp_price = entry_price + (sl_width_calculated * DTS_RRR_DISPLAY)
        rr_ratio = (tp_price - entry_price) / sl_width_calculated # RRRの計算
        
    elif side == 'Short':
        # R1が近い場合はLimit Entryを採用し、SLをR1外側に設定
        if structural_pivot_bonus > 0 and pivot_points.get('r1'):
            entry_price = pivot_points['r1'] # Limit EntryをR1に設定
            # SLはR1よりもATRバッファだけ外側（上）に設定
            sl_price = entry_price + (current_atr * 0.5)
            entry_type = 'Limit'
            structural_sl_used = True
        else:
            # Market Entry
            sl_price = current_price + (current_atr * ATR_TRAIL_MULTIPLIER)
            entry_type = 'Market'
        
        # TP目標はRRR 5.0 で計算
        sl_width_calculated = sl_price - entry_price
        tp_price = entry_price - (sl_width_calculated * DTS_RRR_DISPLAY)
        rr_ratio = (entry_price - tp_price) / sl_width_calculated
        
    else:
        # Neutralシグナルの場合は取引情報なし
        entry_price = current_price
        sl_price = current_price
        tp_price = current_price
        rr_ratio = 0.0
        entry_type = 'N/A'
    
    # 11. 結果の構築
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'price': current_price,
        'entry': entry_price,
        'sl': sl_price,
        'tp1': tp_price,
        'rr_ratio': rr_ratio,
        'entry_type': entry_type,
        'tech_data': {
            'rsi': current_rsi,
            'macd_hist': current_macd_hist,
            'adx': current_adx,
            'atr_value': current_atr,
            'regime': 'Trending' if current_adx >= ADX_TREND_THRESHOLD else 'Ranging',
            'long_term_reversal_penalty': long_term_reversal_penalty,
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
            'volatility_penalty': volatility_penalty,
            'macd_cross_valid': macd_cross_valid,
            'macd_cross_penalty_value': macd_cross_penalty_value,
            'vwap_consistent': (side == 'Long' and current_price > bb_mid) or \
                               (side == 'Short' and current_price < bb_mid) if not np.isnan(bb_mid) else False,
            'stoch_filter_penalty': stoch_filter_penalty,
            'structural_pivot_bonus': structural_pivot_bonus,
            'structural_sl_used': structural_sl_used, # S/RベースのSLが使用されたか
            'pivot_points': pivot_points,
            'volume_confirmation_bonus': volume_confirmation_bonus,
            'volume_ratio': volume_ratio,
            'funding_rate_value': funding_rate_val,
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'dominance_trend': macro_context.get('dominance_trend', 'Neutral'),
            'dominance_bias_bonus_value': dominance_bonus_value
        }
    }


async def analyze_top_symbols(monitor_symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    トップ銘柄の各時間足のシグナルを並行して分析し、統合スコアの高いものを返す
    """
    
    all_signals: List[Dict] = []
    
    async def analyze_symbol(symbol: str):
        # 1. 必要なOHLCVデータを並行して取得
        timeframes = ['15m', '1h', '4h']
        ohlcv_results = await asyncio.gather(*[
            fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf) for tf in timeframes
        ])
        
        signals_for_symbol: List[Dict] = []
        macro_context_with_fr = macro_context.copy()

        # 2. 資金調達率の取得 (1h足の分析時に使用するため)
        # BTC-USDT, ETH-USDT以外の場合のみFRを取得
        if symbol not in ["BTC-USDT", "ETH-USDT"]:
             macro_context_with_fr['funding_rate'] = await fetch_funding_rate(symbol)
        
        # 3. 各時間足の分析を実行
        ohlcv_map = {tf: result[0] for tf, result in zip(timeframes, ohlcv_results)}
        status_map = {tf: result[1] for tf, result in zip(timeframes, ohlcv_results)}
        
        # 4h足のトレンドを先に取得
        signal_4h = analyze_single_timeframe(symbol, '4h', ohlcv_map.get('4h', []), macro_context)
        signals_for_symbol.append(signal_4h)
        long_term_trend_4h = signal_4h.get('tech_data', {}).get('long_term_trend', 'Neutral')
        
        # 1h足の分析
        signal_1h = analyze_single_timeframe(symbol, '1h', ohlcv_map.get('1h', []), macro_context_with_fr)
        signals_for_symbol.append(signal_1h)
        
        # 15m足の分析
        signal_15m = analyze_single_timeframe(symbol, '15m', ohlcv_map.get('15m', []), macro_context_with_fr)
        signals_for_symbol.append(signal_15m)

        # 4. 統合スコアの計算とベストシグナルの決定 (1h足のスコアを採用)
        best_signal = max(signals_for_symbol, key=lambda s: s.get('score', 0.0) if s.get('timeframe') != '4h' else 0.0)
        
        if best_signal.get('score', 0.0) >= SIGNAL_THRESHOLD and best_signal.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]:
            # 最終シグナルとして採用
            best_signal['macro_context'] = macro_context
            best_signal['integrated_signals'] = signals_for_symbol
            all_signals.append(best_signal)

        # 遅延を設けてAPIレート制限を遵守
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 

    # すべてのシンボルに対して並行して分析を実行
    await asyncio.gather(*[analyze_symbol(symbol) for symbol in monitor_symbols])
    
    # スコアの高い順にソートして、TOP SIGNAL COUNTを返す
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    return sorted_signals[:TOP_SIGNAL_COUNT]


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """メインループ: 定期的に市場データを取得し、分析と通知を実行する"""
    global LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    await initialize_ccxt_client()

    while True:
        try:
            # 1. 出来高トップ銘柄リストの更新 (ループ開始時に一度実行)
            if time.time() - LAST_UPDATE_TIME > 60 * 30: # 30分に一度更新
                logging.info("✅ 出来高トップ銘柄リストを更新します。")
                await update_symbols_by_volume()
                LAST_UPDATE_TIME = time.time()
                logging.info(f"✅ モニタリング銘柄: {len(CURRENT_MONITOR_SYMBOLS)} 件")

            # 2. マクロコンテキストの取得 (FR, Dominance, FGI Proxy)
            logging.info("🔬 マクロコンテキスト (FR, Dominance, FGI Proxy) を更新します。")
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            # 3. 監視対象のトップ銘柄を分析
            logging.info(f"🔎 監視対象銘柄 ({len(CURRENT_MONITOR_SYMBOLS)}) の分析を開始します...")
            top_signals = await analyze_top_symbols(CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT) 
            LAST_ANALYSIS_SIGNALS = top_signals
            
            # 4. シグナルの通知とログ出力
            if top_signals:
                logging.info(f"🔔 TOP {len(top_signals)} 件の高スコアシグナルが見つかりました。")
                for rank, signal in enumerate(top_signals, 1):
                    # 前回の通知からクールダウン時間経過後にのみ通知
                    last_notified = TRADE_NOTIFIED_SYMBOLS.get(signal['symbol'], 0.0)
                    if time.time() - last_notified > TRADE_SIGNAL_COOLDOWN:
                        
                        message = format_integrated_analysis_message(signal['symbol'], signal['integrated_signals'], rank)
                        if message:
                            send_telegram_html(message)
                            TRADE_NOTIFIED_SYMBOLS[signal['symbol']] = time.time()
                        
                    else:
                        logging.info(f"クールダウン中のため {signal['symbol']} の通知をスキップします。")
                        
            else:
                logging.info("シグナル閾値 (0.75) を超える取引機会は見つかりませんでした。")
                
            LAST_SUCCESS_TIME = time.time()
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            # CCXTのエラーで取引所に依存するものはループをスキップせずに再試行
            if 'CCXT' in error_name or 'Timeout' in error_name:
                 logging.warning(f"メインループで一時的なエラー: {error_name} - 継続します。")
                 await asyncio.sleep(10)
                 continue
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.4 - KeyError Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.4 Startup initializing...") # バージョン更新
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
        "bot_version": "v17.0.4 - KeyError Fix", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.4)"})


if __name__ == "__main__":
    # 環境変数からポートを取得し、デフォルトは8000
    port = int(os.environ.get("PORT", 8000))
    # ローカル開発やHerokuのGunicorn実行時のために非同期でmain_loopを起動
    uvicorn.run(app, host="0.0.0.0", port=port)
