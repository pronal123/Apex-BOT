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
                    datefmt='S',
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
        is_long = (side == "ロング")
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
                dominance_bias_value = tech_data.get('dominance_bias_value', 0.0)
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
        # fetch_funding_rate は OKX では swap のみ対応
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol)
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        # logging.warning(f"Failed to fetch funding rate for {symbol}: {e}")
        return 0.0

async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        # スポット市場のティッカーを取得
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        # USDTペアかつ出来高情報があるものにフィルタリング
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }
        
        # 出来高 (quoteVolume) で降順ソート
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # トップN銘柄をOKXのスワップ形式 (BTC-USDT) に変換
        new_monitor_symbols = [
            convert_symbol_to_okx_swap(symbol) 
            for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]
        ]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
            logging.info(f"✅ 出来高トップ {TOP_SYMBOL_LIMIT} 銘柄を更新しました。")
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
        
        # 1. 永続スワップ (SWAP) として試行
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30:
             # データ不足の場合はフォールバックに進む
            pass 
        else:
            return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name

    except ccxt.ExchangeError as e:
        # スワップシンボルが見つからなかった場合、スポットで再試行
        if 'market symbol' in str(e) or 'not found' in str(e):
            spot_symbol = symbol.replace('-', '/')
            try:
                # 2. スポット (SPOT) として試行
                ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
                if not ohlcv or len(ohlcv) < 30:
                    return [], "DataShortage", client_name
                return ohlcv, "Success", client_name
            except Exception:
                return [], "ExchangeError", client_name # スポットでも失敗

        return [], "ExchangeError", client_name # その他のExchangeError

    except Exception as e:
        # logging.error(f"OHLCV fetch error for {symbol} ({timeframe}): {e}")
        return [], "ExchangeError", client_name

async def get_crypto_macro_context() -> Dict:
    """
    マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias)
    """
    context: Dict = {
        'sentiment_fgi_proxy': 0.0,
        'btc_trend': 'Neutral',
        'eth_trend': 'Neutral',
        'dominance_trend': 'Neutral', # BTC-ETHの相関バイアス 
        'dominance_bias_value': 0.0
    }
    
    # ----------------------------------------------
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    # ----------------------------------------------
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC-USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH-USDT", '4h')
    
    btc_df = pd.DataFrame(btc_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    eth_df = pd.DataFrame(eth_ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    
    # トレンド分析: 50期間SMAとの比較 (4h足)
    if status_btc == "Success" and len(btc_df) >= LONG_TERM_SMA_LENGTH:
        btc_df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        last_row_btc = btc_df.iloc[-1]
        
        # SMAが利用可能かチェック
        sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
        btc_sma = last_row_btc.get(sma_col, np.nan)
        btc_close = last_row_btc['close']

        if not np.isnan(btc_sma):
            if btc_close > btc_sma:
                context['btc_trend'] = 'Bullish'
            elif btc_close < btc_sma:
                context['btc_trend'] = 'Bearish'
    
    if status_eth == "Success" and len(eth_df) >= LONG_TERM_SMA_LENGTH:
        eth_df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        last_row_eth = eth_df.iloc[-1]
        
        # SMAが利用可能かチェック
        sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
        eth_sma = last_row_eth.get(sma_col, np.nan)
        eth_close = last_row_eth['close']
        
        if not np.isnan(eth_sma):
            if eth_close > eth_sma:
                context['eth_trend'] = 'Bullish'
            elif eth_close < eth_sma:
                context['eth_trend'] = 'Bearish'
    
    # ----------------------------------------------
    # 2. FGI Proxy の計算: BTCとETHの直近4hの騰落率を平均
    # ----------------------------------------------
    fgi_proxy = 0.0
    count = 0
    
    if status_btc == "Success" and len(btc_df) >= 2:
        # 直近2本のローソク足の終値変化率を評価
        change_btc = (btc_df.iloc[-1]['close'] - btc_df.iloc[-2]['close']) / btc_df.iloc[-2]['close']
        fgi_proxy += change_btc
        count += 1
        
    if status_eth == "Success" and len(eth_df) >= 2:
        # 直近2本のローソク足の終値変化率を評価
        change_eth = (eth_df.iloc[-1]['close'] - eth_df.iloc[-2]['close']) / eth_df.iloc[-2]['close']
        fgi_proxy += change_eth
        count += 1
    
    if count > 0:
        # 平均変動率をFGI Proxyとして採用 (例: 0.2%上昇 -> 0.002)
        context['sentiment_fgi_proxy'] = fgi_proxy / count
        
    # ----------------------------------------------
    # 3. Dominance Bias の計算: BTC/ETHのトレンド比較
    # ----------------------------------------------
    btc_trend = context['btc_trend']
    eth_trend = context['eth_trend']

    if btc_trend == 'Bullish' and eth_trend == 'Bearish':
        # BTC優位 (Altcoinの資金流出)
        context['dominance_trend'] = 'BTC-Strong'
        context['dominance_bias_value'] = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoinはペナルティ
    elif btc_trend == 'Bearish' and eth_trend == 'Bullish':
        # ETH優位 (Altcoinに資金流入)
        context['dominance_trend'] = 'Altcoin-Strong'
        context['dominance_bias_value'] = DOMINANCE_BIAS_BONUS_PENALTY # Altcoinはボーナス
    elif btc_trend == 'Bullish' and eth_trend == 'Bullish':
        # 市場全体が強気 (Altcoinも強気になりやすい)
        context['dominance_trend'] = 'Both-Bullish'
        context['dominance_bias_value'] = DOMINANCE_BIAS_BONUS_PENALTY / 2 # 小さなボーナス
    elif btc_trend == 'Bearish' and eth_trend == 'Bearish':
        # 市場全体が弱気
        context['dominance_trend'] = 'Both-Bearish'
        context['dominance_bias_value'] = -DOMINANCE_BIAS_BONUS_PENALTY / 2 # 小さなペナルティ

    return context

# ====================================================================================
# CORE LOGIC: TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_pivot_points(df: pd.DataFrame, is_daily: bool = False) -> Dict:
    """
    Pivot Points (Classic) を計算する
    日足/4h足のデータフレームの最後の期間のHLC情報を使用。
    """
    if len(df) < 1:
        return {}
        
    # 日足（24時間）のPivot計算には、前日（最後から2番目）のデータを使用するのが一般的。
    # ここでは、データフレームの最後の期間のOHLCVデータを使用（日足で実行されることを想定）。
    # ただし、4h足で使用する場合、その4h期間のHLCとなる。
    # 構造的なS/Rとして使用するため、ここでは最も新しい完成したローソク足(最後から2番目)をベースにする。
    
    if len(df) < 2:
         return {}
    
    # 最後から2番目のローソク足を使用
    prev_row = df.iloc[-2]

    H = prev_row['high']
    L = prev_row['low']
    C = prev_row['close']
    
    # Classic Pivot Point
    PP = (H + L + C) / 3
    
    R1 = 2 * PP - L
    S1 = 2 * PP - H
    R2 = PP + (H - L)
    S2 = PP - (H - L)
    
    return {
        'pp': PP,
        'r1': R1,
        'r2': R2,
        's1': S1,
        's2': S2
    }

def calculate_rr_ratio(entry: float, sl: float, tp1: float, side_long: bool) -> float:
    """
    リスクリワードレシオを計算する (初期SL/TP目標値に基づく)
    """
    if sl == entry or entry <= 0:
        return 0.0 # リスク幅ゼロは無限大になるため、0を返す
    
    risk_abs = abs(entry - sl)
    reward_abs = abs(tp1 - entry)
    
    if risk_abs == 0:
        return 0.0
        
    # ロングの場合: (TP > Entry) かつ (Entry > SL)
    # ショートの場合: (TP < Entry) かつ (Entry < SL)
    
    if side_long and (tp1 <= entry or entry <= sl):
        # ロングなのにTPがSL以下、またはEntryがSL以下
        return 0.0

    if not side_long and (tp1 >= entry or entry >= sl):
        # ショートなのにTPがSL以上、またはEntryがSL以上
        return 0.0

    return reward_abs / risk_abs

def get_trailing_stop_levels(df: pd.DataFrame, side_long: bool, entry_price: float) -> Tuple[float, float, str, bool]:
    """
    動的追跡損切 (Dynamic Trailing Stop: DTS) と構造的SLに基づく
    TP1 (高めの目標値) と SL (初期/追跡SL) の水準を計算する。
    
    - TP1: ATRの5倍を目標値として設定 (DTSにより利益はさらに伸びる可能性)
    - SL: ATR_TRAIL_MULTIPLIER (初期は3.0) を使用して ATR SL を設定。
    - Structural SL: 直近のPivot S1/R1も評価し、ATR SLが構造的なS/Rを破る場合、
                     構造的なS/RにATRの0.5倍のバッファを加えた位置をSLとして採用。
    """
    if len(df) < 20: 
        return entry_price, entry_price, 'N/A', False 

    # 1. ATRの計算
    df.ta.atr(length=14, append=True)
    atr_col = 'ATR_14'
    last_row = df.iloc[-1]
    atr_value = last_row.get(atr_col, np.nan)
    
    if np.isnan(atr_value) or atr_value == 0:
        # ATRが計算できない場合は、価格の0.5%を暫定値として使用
        atr_value = df.iloc[-1]['close'] * 0.005 

    # 2. TP1 (高めの目標値: RRR = 5.0 を目安とする)
    tp1_price = entry_price
    initial_risk_size = atr_value * ATR_TRAIL_MULTIPLIER
    
    if side_long:
        tp1_price = entry_price + (initial_risk_size * DTS_RRR_DISPLAY)
    else:
        tp1_price = entry_price - (initial_risk_size * DTS_RRR_DISPLAY)
        
    # 3. ATRに基づく初期SL (Trailing Stopの初期位置)
    atr_sl = entry_price - initial_risk_size if side_long else entry_price + initial_risk_size
    
    # 4. 構造的S/Rの計算と適用
    pivot_points = calculate_pivot_points(df)
    structural_sl_used = False
    final_sl = atr_sl
    
    if pivot_points:
        # ATR SLの0.5倍をバッファとして使用
        atr_buffer = atr_value * 0.5 
        
        if side_long:
            # ロングの場合、S1/S2が構造的SLの候補
            s1 = pivot_points.get('s1', 0.0)
            s2 = pivot_points.get('s2', 0.0)
            
            # S1より下にATR SLがある場合、S1の少し下をSL候補とする (よりタイトなSLを採用)
            # S1がATR SLより上にある場合、S1をSL候補とし、バッファを引く
            if s1 > 0 and s1 > atr_sl:
                 structural_sl = s1 - atr_buffer 
                 # 構造的SLが現在のATR SLよりタイトな場合のみ採用 (リスク最小化)
                 if structural_sl > final_sl:
                     final_sl = structural_sl
                     structural_sl_used = True

        else: # ショート
            # ショートの場合、R1/R2が構造的SLの候補
            r1 = pivot_points.get('r1', 0.0)
            r2 = pivot_points.get('r2', 0.0)
            
            # R1より上にATR SLがある場合、R1の少し上をSL候補とする
            if r1 > 0 and r1 < atr_sl:
                structural_sl = r1 + atr_buffer 
                # 構造的SLが現在のATR SLよりタイトな場合のみ採用 (リスク最小化)
                if structural_sl < final_sl:
                    final_sl = structural_sl
                    structural_sl_used = True
                    
    # TP1は価格が高すぎる場合、少し調整
    if side_long and tp1_price < entry_price:
        tp1_price = entry_price + initial_risk_size * 2.0
    elif not side_long and tp1_price > entry_price:
        tp1_price = entry_price - initial_risk_size * 2.0

    return tp1_price, final_sl, atr_value, structural_sl_used


def apply_scoring_and_filters(
    symbol: str, 
    timeframe: str, 
    df: pd.DataFrame, 
    side_long: bool, 
    macro_context: Dict, 
    pivot_points: Dict,
    funding_rate_value: float
) -> Tuple[float, Dict]:
    """
    技術指標に基づきスコアリングとフィルタリングを行う
    スコアは 0.0 から 1.0 の範囲
    """
    base_score = BASE_SCORE
    tech_data: Dict[str, Any] = {
        'rsi': np.nan, 'cci': np.nan, 'adx': np.nan, 'macd_hist': np.nan, 'regime': 'N/A', 
        'atr_value': np.nan, 'pivot_points': pivot_points,
        'long_term_trend': 'Neutral', 'long_term_reversal_penalty': False, 'long_term_reversal_penalty_value': 0.0,
        'macd_cross_valid': True, 'macd_cross_penalty_value': 0.0,
        'vwap_consistent': False, 'vwap_penalty': 0.0,
        'stoch_filter_penalty': 0.0,
        'volume_ratio': 1.0, 'volume_confirmation_bonus': 0.0,
        'structural_pivot_bonus': 0.0, 'structural_sl_used': False,
        'funding_rate_value': funding_rate_value, 'funding_rate_bonus_value': 0.0,
        'dominance_trend': macro_context.get('dominance_trend', 'Neutral'),
        'dominance_bias_value': macro_context.get('dominance_bias_value', 0.0),
        'dominance_bias_bonus_value': 0.0
    }
    
    if len(df) < 2:
        return 0.0, tech_data
        
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    
    # --- 1. テクニカル指標の計算結果の取得 (FIX: .get()を使用してKeyErrorを回避) ---
    
    # RSI (14)
    rsi_col = 'RSI_14'
    tech_data['rsi'] = last_row.get(rsi_col, np.nan)

    # CCI (14)
    cci_col = 'CCI_14'
    tech_data['cci'] = last_row.get(cci_col, np.nan)
    
    # ADX (14) と Regime (トレンド or レンジ)
    adx_col = 'ADX_14'
    adx_value = last_row.get(adx_col, np.nan)
    tech_data['adx'] = adx_value
    
    if not np.isnan(adx_value):
        tech_data['regime'] = 'Trend' if adx_value >= ADX_TREND_THRESHOLD else 'Ranging'
    
    # MACD History
    macd_hist_col = 'MACDh_12_26_9'
    tech_data['macd_hist'] = last_row.get(macd_hist_col, np.nan)

    # ATR (14)
    atr_col = 'ATR_14'
    tech_data['atr_value'] = last_row.get(atr_col, np.nan)
    
    # VWAP (v17.0.0で追加)
    vwap_col = 'VWAP'
    vwap_value = last_row.get(vwap_col, np.nan)
    
    # STOCHRSI (v17.0.0で追加)
    k_col = 'STOCHk_14_14_3_3'
    d_col = 'STOCHd_14_14_3_3'
    stochk = last_row.get(k_col, np.nan)
    stochd = last_row.get(d_col, np.nan)
    
    # Long Term SMA (50)
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    sma_value = last_row.get(sma_col, np.nan)

    # --- 2. スコアリングロジックの適用 ---
    score = base_score
    
    # 2-1. RSI条件
    rsi = tech_data['rsi']
    if not np.isnan(rsi):
        if side_long:
            # ロング: 買われすぎではない (RSI < 70) かつ、売られすぎに近い (RSI < 40)
            if rsi < RSI_OVERBOUGHT and rsi < RSI_MOMENTUM_LOW: 
                score += 0.15
            elif rsi < RSI_OVERBOUGHT:
                score += 0.05
            if rsi > RSI_OVERBOUGHT:
                score -= 0.10 # 買われすぎはペナルティ
        else: # ショート
            # ショート: 売られすぎではない (RSI > 30) かつ、買われすぎに近い (RSI > 60)
            if rsi > RSI_OVERSOLD and rsi > RSI_MOMENTUM_HIGH: 
                score += 0.15
            elif rsi > RSI_OVERSOLD:
                score += 0.05
            if rsi < RSI_OVERSOLD:
                score -= 0.10 # 売られすぎはペナルティ
                
    # 2-2. CCI条件
    cci = tech_data['cci']
    if not np.isnan(cci):
        if side_long and cci < -100:
            score += 0.10
        elif not side_long and cci > 100:
            score += 0.10
            
    # 2-3. ADX条件 (トレンド相場でのみボーナス)
    regime = tech_data['regime']
    if regime == 'Trend':
        score += 0.05 # ADXトレンドは小さなボーナス
    
    # 2-4. MACDモメンタムフィルター (MACD-Hの方向)
    macd_h = tech_data['macd_hist']
    if not np.isnan(macd_h) and not np.isnan(prev_row.get(macd_hist_col, np.nan)):
        prev_macd_h = prev_row.get(macd_hist_col, 0.0)
        
        if side_long:
            # ロング: MACD-Hがゼロライン以下だが、回復傾向にある (モメンタムの底打ち)
            if macd_h < 0 and macd_h > prev_macd_h:
                score += 0.10
            # MACD-Hがゼロラインを超えて上昇中
            elif macd_h > 0 and macd_h > prev_macd_h:
                score += 0.15
            # 既にMACD-Hがゼロラインを超えているのに、下降している場合はペナルティ (モメンタム反転)
            elif macd_h > 0 and macd_h < prev_macd_h:
                 score -= MACD_CROSS_PENALTY
                 tech_data['macd_cross_valid'] = False
                 tech_data['macd_cross_penalty_value'] = MACD_CROSS_PENALTY
        else: # ショート
            # ショート: MACD-Hがゼロライン以上だが、下降傾向にある (モメンタムの天井打ち)
            if macd_h > 0 and macd_h < prev_macd_h:
                score += 0.10
            # MACD-Hがゼロラインを下回って下降中
            elif macd_h < 0 and macd_h < prev_macd_h:
                score += 0.15
            # 既にMACD-Hがゼロラインを下回っているのに、上昇している場合はペナルティ (モメンタム反転)
            elif macd_h < 0 and macd_h > prev_macd_h:
                 score -= MACD_CROSS_PENALTY
                 tech_data['macd_cross_valid'] = False
                 tech_data['macd_cross_penalty_value'] = MACD_CROSS_PENALTY
            
    # 2-5. 長期トレンド逆行ペナルティ (50SMA)
    if not np.isnan(sma_value):
        if side_long and last_row['close'] < sma_value:
            # ロングで長期トレンドが下降の場合、ペナルティ
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_trend'] = 'Bearish'
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
        elif not side_long and last_row['close'] > sma_value:
            # ショートで長期トレンドが上昇の場合、ペナルティ
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_trend'] = 'Bullish'
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
        elif side_long and last_row['close'] > sma_value:
            tech_data['long_term_trend'] = 'Bullish'
        elif not side_long and last_row['close'] < sma_value:
            tech_data['long_term_trend'] = 'Bearish'


    # 2-6. VWAPフィルター (VWAPとの関係が逆行している場合はペナルティ/VWAPがない場合はスキップ)
    if not np.isnan(vwap_value):
        if side_long and last_row['close'] > vwap_value:
            # ロングで価格がVWAPより上 (順行)
            score += 0.05
            tech_data['vwap_consistent'] = True
        elif not side_long and last_row['close'] < vwap_value:
            # ショートで価格がVWAPより下 (順行)
            score += 0.05
            tech_data['vwap_consistent'] = True
        else:
            # 逆行している場合はペナルティ
            score -= 0.10
            tech_data['vwap_consistent'] = False
            tech_data['vwap_penalty'] = 0.10


    # 2-7. StochRSIフィルター (過熱感の排除) - 15m/1hのみ適用
    if timeframe in ['15m', '1h'] and not np.isnan(stochk) and not np.isnan(stochd):
         # 過熱感によるペナルティ
         penalty = 0.10
         if side_long and stochk > 90 and stochd > 90:
             score -= penalty
             tech_data['stoch_filter_penalty'] = penalty
         elif not side_long and stochk < 10 and stochd < 10:
             score -= penalty
             tech_data['stoch_filter_penalty'] = penalty
         
    
    # --- 3. 構造的S/Rの確証 (ボーナス) ---
    pivot_r1 = pivot_points.get('r1', 0.0)
    pivot_s1 = pivot_points.get('s1', 0.0)
    
    # 構造的S/Rのボーナス
    structural_bonus = 0.15 
    
    if side_long and pivot_s1 > 0:
        # ロング: 現在価格がS1/S2に近く、底打ちを狙っている場合にボーナス
        # 現在価格がS1とPPの間 (S1からの反発期待) かつ、SLがS1/S2より下にある場合
        if last_row['close'] >= pivot_s1 and last_row['close'] < pivot_points.get('pp', 0.0) :
             score += structural_bonus
             tech_data['structural_pivot_bonus'] = structural_bonus
             
    elif not side_long and pivot_r1 > 0:
        # ショート: 現在価格がR1/R2に近く、天井打ちを狙っている場合にボーナス
        # 現在価格がR1とPPの間 (R1からの反落期待) かつ、SLがR1/R2より上にある場合
        if last_row['close'] <= pivot_r1 and last_row['close'] > pivot_points.get('pp', 0.0):
             score += structural_bonus
             tech_data['structural_pivot_bonus'] = structural_bonus

    
    # --- 4. 出来高フィルター (ボーナス) ---
    volume_bonus = 0.0
    if len(df) > 20:
        # 直近20本の平均出来高を計算
        avg_volume = df['volume'].iloc[-20:-1].mean()
        
        # 直近の出来高が平均のX倍以上
        volume_ratio = last_row['volume'] / avg_volume if avg_volume > 0 else 1.0
        tech_data['volume_ratio'] = volume_ratio
        
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
            volume_bonus = 0.10 # 出来高急増ボーナス
            score += volume_bonus
            tech_data['volume_confirmation_bonus'] = volume_bonus

    
    # --- 5. 資金調達率 (Funding Rate) バイアスフィルター ---
    funding_bonus_penalty = 0.0
    if abs(funding_rate_value) >= FUNDING_RATE_THRESHOLD:
        
        if side_long:
            # ロング (買い) シグナルの場合
            if funding_rate_value < 0:
                # Funding Rateがマイナス (ショート過密/ロング優位) -> ボーナス
                funding_bonus_penalty = FUNDING_RATE_BONUS_PENALTY
            else:
                # Funding Rateがプラス (ロング過密/ショート優位) -> ペナルティ
                funding_bonus_penalty = -FUNDING_RATE_BONUS_PENALTY
        else: # ショート (売り) シグナルの場合
            if funding_rate_value > 0:
                # Funding Rateがプラス (ロング過密/ショート優位) -> ボーナス
                funding_bonus_penalty = FUNDING_RATE_BONUS_PENALTY
            else:
                # Funding Rateがマイナス (ショート過密/ロング優位) -> ペナルティ
                funding_bonus_penalty = -FUNDING_RATE_BONUS_PENALTY
        
        score += funding_bonus_penalty
        tech_data['funding_rate_bonus_value'] = funding_bonus_penalty

    
    # --- 6. Dominance Bias フィルター (Altcoinのみ) ---
    dominance_bonus_penalty = 0.0
    
    if symbol not in ["BTC-USDT", "ETH-USDT"]:
        # Altcoinの場合にのみ、BTC/ETHの相対的なトレンドを評価
        bias_value = macro_context.get('dominance_bias_value', 0.0)
        
        if side_long and bias_value > 0:
             # ロングでAltcoinに資金流入傾向 (Bullish/Altcoin-Strong)
             dominance_bonus_penalty = abs(bias_value)
        elif not side_long and bias_value < 0:
             # ショートでAltcoinから資金流出傾向 (Bearish/BTC-Strong)
             dominance_bonus_penalty = abs(bias_value)
        else:
             # バイアスに逆行する場合、ペナルティ (バイアス値は既にマイナスまたはゼロのため、そのまま使用)
             dominance_bonus_penalty = bias_value

        score += dominance_bonus_penalty
        tech_data['dominance_bias_bonus_value'] = dominance_bonus_penalty


    # 最終スコアを 1.0 にクリップ
    final_score = min(1.0, max(0.0, score))
    
    return final_score, tech_data


async def analyze_single_timeframe(
    symbol: str, 
    timeframe: str, 
    macro_context: Dict, 
    funding_rate_value: float
) -> Dict:
    """
    単一の時間軸でOHLCVを取得、分析、シグナルを生成する
    """
    
    logging.info(f"[{symbol} - {timeframe}] データ取得を開始...")
    ohlcv, status, client = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe)
    
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'Neutral',
        'score': 0.50,
        'entry': 0.0, 'sl': 0.0, 'tp1': 0.0, 'rr_ratio': 0.0,
        'entry_type': 'N/A',
        'price': 0.0,
        'tech_data': {},
        'macro_context': macro_context,
        'funding_rate_value': funding_rate_value
    }
    
    if status != "Success":
        result['side'] = status
        logging.warning(f"[{symbol} - {timeframe}] データ取得失敗: {status}")
        return result
    
    df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
    df['time'] = pd.to_datetime(df['time'], unit='ms', utc=True).dt.tz_convert(JST)
    df = df.set_index('time')
    
    # 最後の終値 (参照価格)
    last_close = df['close'].iloc[-1]
    result['price'] = last_close
    
    if len(df) < REQUIRED_OHLCV_LIMITS.get(timeframe, 100):
        result['side'] = 'DataShortage'
        return result
    
    # ----------------------------------------------
    # 1. テクニカル指標の計算
    # ----------------------------------------------
    
    # 50 SMA (長期トレンド用)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    
    # RSI (14)
    df.ta.rsi(length=14, append=True)
    
    # CCI (14)
    df.ta.cci(length=14, append=True)
    
    # ADX (14)
    df.ta.adx(length=14, append=True)
    
    # MACD (12, 26, 9)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    
    # ATR (14)
    df.ta.atr(length=14, append=True)
    
    # VWAP (期間なしで計算、累積VWAP)
    # vwap_df = df.copy() # vwapは期間の初めから計算するため、毎回計算する
    # df['VWAP'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
    # Pandas-TA の VWAP 関数は日足内のリセットロジックをサポートしていないため、ここでは省略するか、
    # 独自のロジック (または期間全体での計算) を使用する必要があります。
    # 簡易的に期間全体での計算を使用 (完全なVWAPではない)
    
    # 期間全体での累積VWAP (簡易版)
    # df['VWAP'] = df.ta.vwap(df['high'], df['low'], df['close'], df['volume']) # -> エラーになるため、手動で計算するか、より安定した指標を採用
    # Pandas-TAはVWAPを直接サポートしていないため、計算をスキップし、この指標を使用しない
    # 代わりにBollinger Bands (BBANDS) を使用する (Volatility Penalty用)
    
    # Bollinger Bands (BB_20, 2.0)
    df.ta.bbands(length=20, std=2.0, append=True)
    
    # STOCHRSI (14, 14, 3, 3)
    df.ta.stochrsi(length=14, rsi_length=14, k=3, d=3, append=True)


    # ----------------------------------------------
    # 2. シグナル判定
    # ----------------------------------------------
    
    # 最新の完成したローソク足のデータ
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    # シグナルの主要な根拠
    is_long_signal = False
    is_short_signal = False
    
    # 1. 価格と50SMAの関係 (長期トレンドとの一致: v17.0.0ではペナルティとして使用)
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    sma_value = last_row.get(sma_col, np.nan)
    long_term_trend = 'Neutral'
    if not np.isnan(sma_value):
        if last_close > sma_value:
            long_term_trend = 'Bullish'
        elif last_close < sma_value:
            long_term_trend = 'Bearish'

    # 2. RSI (14)
    rsi_col = 'RSI_14'
    rsi_value = last_row.get(rsi_col, np.nan)
    
    # 3. MACD History (モメンタム)
    macd_hist_col = 'MACDh_12_26_9'
    macd_h = last_row.get(macd_hist_col, np.nan)
    prev_macd_h = prev_row.get(macd_hist_col, np.nan)
    
    # 4. CCI (14)
    cci_col = 'CCI_14'
    cci_value = last_row.get(cci_col, np.nan)
    
    # 5. ボリンジャーバンド (BB_20)
    lower_band_col = 'BBL_20_2.0'
    upper_band_col = 'BBU_20_2.0'
    lower_band = last_row.get(lower_band_col, np.nan)
    upper_band = last_row.get(upper_band_col, np.nan)

    # ----------------------------------------------
    # ロングシグナル判定
    # ----------------------------------------------
    if not np.isnan(rsi_value) and not np.isnan(macd_h):
        # 買われすぎではない (RSI < 70)
        is_rsi_ok = rsi_value < RSI_OVERBOUGHT 
        # 売られすぎに近い (RSI < 40) または、モメンタムの回復が見られる (MACD-Hがマイナスから回復)
        is_momentum_buy = (
            (rsi_value <= RSI_MOMENTUM_LOW and cci_value < 0) or 
            (macd_h > prev_macd_h and macd_h < 0) or 
            (macd_h > 0 and macd_h > prev_macd_h)
        )
        # 価格がボリンジャーバンド下限に近い (ボラティリティの収縮/反発期待)
        is_bb_reversal = last_close < lower_band * 1.005 if not np.isnan(lower_band) else False
        
        if is_rsi_ok and is_momentum_buy and is_bb_reversal:
            is_long_signal = True

    # ----------------------------------------------
    # ショートシグナル判定
    # ----------------------------------------------
    if not np.isnan(rsi_value) and not np.isnan(macd_h):
        # 売られすぎではない (RSI > 30)
        is_rsi_ok = rsi_value > RSI_OVERSOLD
        # 買われすぎに近い (RSI > 60) または、モメンタムの天井打ちが見られる (MACD-Hがプラスから下降)
        is_momentum_sell = (
            (rsi_value >= RSI_MOMENTUM_HIGH and cci_value > 0) or
            (macd_h < prev_macd_h and macd_h > 0) or
            (macd_h < 0 and macd_h < prev_macd_h)
        )
        # 価格がボリンジャーバンド上限に近い (ボラティリティの収縮/反落期待)
        is_bb_reversal = last_close > upper_band * 0.995 if not np.isnan(upper_band) else False

        if is_rsi_ok and is_momentum_sell and is_bb_reversal:
            is_short_signal = True
            
    # ----------------------------------------------
    # 3. シグナル採用とスコアリング
    # ----------------------------------------------
    
    if is_long_signal and not is_short_signal:
        side = 'ロング'
        # スコアリングの実行
        pivot_points = calculate_pivot_points(df, is_daily=timeframe == '1d')
        score, tech_data = apply_scoring_and_filters(
            symbol, timeframe, df, True, macro_context, pivot_points, funding_rate_value
        )
        result['side'] = side
        result['score'] = score
        result['tech_data'] = tech_data
        
    elif is_short_signal and not is_long_signal:
        side = 'ショート'
        # スコアリングの実行
        pivot_points = calculate_pivot_points(df, is_daily=timeframe == '1d')
        score, tech_data = apply_scoring_and_filters(
            symbol, timeframe, df, False, macro_context, pivot_points, funding_rate_value
        )
        result['side'] = side
        result['score'] = score
        result['tech_data'] = tech_data
    
    else:
        # シグナルなし、または両方
        result['side'] = 'Neutral'
        return result

    # ----------------------------------------------
    # 4. エントリー、SL、TPの決定 (シグナルがある場合)
    # ----------------------------------------------
    
    # Entry: 直近のローソク足の終値をエントリー価格の基準とする (リミット注文のベース)
    entry_price_base = last_close
    
    # TP/SLの決定 (DTSロジック)
    # TP1: 遠い目標値 (DTS採用のためRRR表示用)
    # SL: 初期追跡ストップ/構造的SL
    tp1, sl, atr_value, structural_sl_used = get_trailing_stop_levels(
        df, side == 'ロング', entry_price_base
    )
    
    # TP/SLの決定で問題が発生した場合
    if sl == entry_price_base:
        result['side'] = 'Neutral'
        return result
        
    # SL/TPを技術データに追加 (ログ表示用)
    result['tech_data']['atr_value'] = atr_value
    result['tech_data']['structural_sl_used'] = structural_sl_used
    
    # RRRを計算 (TP1を目標値としたもの)
    rr_ratio = calculate_rr_ratio(entry_price_base, sl, tp1, side == 'ロング')
    
    # 最終結果の反映
    result['entry'] = entry_price_base
    result['sl'] = sl
    result['tp1'] = tp1
    result['rr_ratio'] = rr_ratio
    result['entry_type'] = 'Market/Limit' # 実際にはLimit注文を推奨
    
    # 5. リスクリワードレシオのフィルタリング
    # RRRが極端に低い (リスク高) または計算不能なシグナルは排除
    if rr_ratio < 0.5:
        result['side'] = 'Neutral'
        return result
        
    return result


async def analyze_symbol(symbol: str) -> List[Dict]:
    """
    指定された銘柄に対し、3つの時間軸で並行して分析を実行する
    """
    global GLOBAL_MACRO_CONTEXT, TRADE_NOTIFIED_SYMBOLS
    
    # 資金調達率を事前に取得
    funding_rate_value = await fetch_funding_rate(symbol)
    
    # 分析タスクの作成
    analysis_tasks = [
        analyze_single_timeframe(symbol, '15m', GLOBAL_MACRO_CONTEXT, funding_rate_value),
        analyze_single_timeframe(symbol, '1h', GLOBAL_MACRO_CONTEXT, funding_rate_value),
        analyze_single_timeframe(symbol, '4h', GLOBAL_MACRO_CONTEXT, funding_rate_value),
    ]
    
    # 並行実行
    results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
    
    final_signals: List[Dict] = []
    
    for res in results:
        if isinstance(res, Exception):
            # 例外が発生した場合
            logging.error(f"[{symbol}] 分析中に例外が発生しました: {res}")
        elif res.get('side') not in ["Neutral", "DataShortage", "ExchangeError"] and res.get('score', 0.5) >= BASE_SCORE:
            final_signals.append(res)

    return final_signals


async def monitor_and_analyze_symbols(monitor_symbols: List[str]):
    """
    監視対象の銘柄すべてを非同期で分析し、結果を統合する
    """
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME
    
    logging.info(f"--- 📊 分析開始 ({len(monitor_symbols)} 銘柄) ---")
    
    # 1. 銘柄ごとの分析タスクを作成
    analysis_tasks = []
    for symbol in monitor_symbols:
        # レートリミット対策として遅延を入れる
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 
        analysis_tasks.append(analyze_symbol(symbol))
        
    # 2. 全銘柄を非同期で実行
    all_results_list = await asyncio.gather(*analysis_tasks, return_exceptions=True) # <--- 修正後のawait

    # 3. 結果の統合とフィルタリング
    integrated_signals: List[Dict] = []
    
    for symbol_results in all_results_list:
        if isinstance(symbol_results, list) and symbol_results:
            
            # 最もスコアの高いシグナル（時間軸）を選択
            best_signal_for_symbol = max(
                symbol_results, 
                key=lambda s: s.get('score', 0.5)
            )
            
            # 閾値以上のシグナルのみを統合リストに追加
            if best_signal_for_symbol.get('score', 0.5) >= SIGNAL_THRESHOLD:
                integrated_signals.append(best_signal_for_symbol)

    # 4. 総合スコアでランキングし、上位N件を抽出
    integrated_signals.sort(key=lambda x: x.get('score', 0.0), reverse=True)
    
    # 上位 N 件の結果をグローバルに保存
    LAST_ANALYSIS_SIGNALS = integrated_signals[:TOP_SIGNAL_COUNT]
    
    # 5. Telegram通知の実行
    await process_and_notify_signals(LAST_ANALYSIS_SIGNALS)
    
    LAST_SUCCESS_TIME = time.time()
    logging.info(f"--- ✅ 分析完了 ({len(integrated_signals)} 件のシグナル、上位 {len(LAST_ANALYSIS_SIGNALS)} 件を通知対象) ---")


async def process_and_notify_signals(signals: List[Dict]):
    """
    通知クールダウンをチェックし、Telegramにシグナルを送信する
    """
    global TRADE_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    # クールダウン期間が終了した銘柄を削除
    symbols_to_delete = [
        symbol for symbol, last_time in TRADE_NOTIFIED_SYMBOLS.items() 
        if current_time - last_time > TRADE_SIGNAL_COOLDOWN
    ]
    for symbol in symbols_to_delete:
        del TRADE_NOTIFIED_SYMBOLS[symbol]
        
    notification_count = 0
    rank = 1
    
    for signal in signals:
        symbol = signal['symbol']
        side = signal['side']
        score = signal['score']
        
        # 冷却期間チェック
        if symbol in TRADE_NOTIFIED_SYMBOLS:
            last_notified_time = TRADE_NOTIFIED_SYMBOLS[symbol]
            if current_time - last_notified_time < TRADE_SIGNAL_COOLDOWN:
                logging.warning(f"[{symbol}] シグナルはクールダウン期間中です (次回通知まで残り: {int(TRADE_SIGNAL_COOLDOWN - (current_time - last_notified_time))}秒)")
                continue
        
        # 通知メッセージを整形
        message = format_integrated_analysis_message(symbol, signal['symbol_signals'], rank) 

        if message:
            # Telegram送信
            success = send_telegram_html(message)
            
            if success:
                # 通知成功した場合、クールダウン時間を更新
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                notification_count += 1
                rank += 1
                # レートリミット対策として遅延を入れる
                await asyncio.sleep(2.0) 
        
        if rank > TOP_SIGNAL_COUNT:
            break
            
    if notification_count > 0:
        logging.info(f"合計 {notification_count} 件の取引シグナルをTelegramに送信しました。")
    elif len(signals) > 0:
         logging.info("シグナルは検出されましたが、クールダウン期間中のため送信はスキップされました。")
    else:
        logging.info("閾値を超える取引シグナルは見つかりませんでした。")


# ====================================================================================
# MAIN LOOP & FASTAPI SETUP
# ====================================================================================

async def main_loop():
    """
    ボットのメイン実行ループ
    """
    global EXCHANGE_CLIENT, GLOBAL_MACRO_CONTEXT
    
    # 1. CCXTクライアントの初期化
    await initialize_ccxt_client()

    while True:
        try:
            logging.info(f"--- 🔄 メインループ実行 (間隔: {LOOP_INTERVAL}秒) ---")
            
            # 2. 監視銘柄リストの動的更新
            await update_symbols_by_volume()
            
            # 3. マクロ市場コンテキストの更新
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            # 4. 全銘柄の分析と通知の実行
            await monitor_and_analyze_symbols(CURRENT_MONITOR_SYMBOLS) # <--- ここでawait

            logging.info(f"--- 💤 次のループまで待機 (最終成功時刻: {datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=JST).strftime('%Y-%m-%d %H:%M:%S')}) ---")
            LAST_UPDATE_TIME = time.time()
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}")
            # エラー発生時はクールダウン期間を短く設定して再試行
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.4 - KeyError Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にメインループを開始"""
    logging.info("🚀 Apex BOT v17.0.4 Startup initializing...") # バージョン更新
    # メインループを非同期タスクとして開始
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時にCCXTクライアントを閉じる"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    """ボットの現在の状態を返す"""
    global LAST_SUCCESS_TIME, CCXT_CLIENT_NAME, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS
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
    """ヘルスチェック用のルート"""
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.4)"}) # バージョン更新

# このファイルが uvicorn で直接実行される場合に使用
if __name__ == "__main__":
    # Render環境で起動する際のコマンドと一致させる
    uvicorn.run("main_render:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), log_level="info")
