# ====================================================================================
# Apex BOT v17.0.11 - FIX: BTC Dominance MultiIndex Error
# - FIX: yfinanceから取得したデータがMultiIndexを返し、その後の処理でエラーとなる問題を解決。
#        データフレームのインデックスと列を明示的に平坦化するロジックを追加。
# - v17.0.10: 通知不成立時の通知ロジックを維持。
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
ATR_LENGTH = 14 
SMA_LENGTH = 20 

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 

# V17.0.5 CUSTOM: New Trading Size & Dynamic TP Parameters (User Request 1 & 2)
TRADE_SIZE_USD = 1000.0             
POSITION_CAPITAL = TRADE_SIZE_USD   
TP_ATR_MULTIPLIERS = {             
    '15m': 1.8, 
    '1h': 2.2,  
    '4h': 3.0,  
}
TREND_CONSISTENCY_BONUS = 0.10      

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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.8)
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
    sl_price = best_signal.get('sl', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A') 

    dynamic_tp_price = best_signal.get('dynamic_tp_price', 0.0) 
    tp_multiplier_used = best_signal.get('tp_multiplier_used', ATR_TRAIL_MULTIPLIER)
    
    tp_price_for_pnl = dynamic_tp_price 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    sl_width_calculated = abs(entry_price - sl_price)
    
    is_long = (side == "ロング")
    sl_loss_usd = 0.0
    tp_gain_usd = 0.0
    sl_risk_percent = 0.0
    tp_gain_percent = 0.0

    if entry_price > 0 and sl_price > 0 and tp_price_for_pnl > 0:
        quantity = POSITION_CAPITAL / entry_price
        
        sl_risk_usd_abs = quantity * abs(entry_price - sl_price)
        tp_gain_usd_abs = quantity * abs(entry_price - tp_price_for_pnl) 
        
        sl_risk_percent = (sl_risk_usd_abs / POSITION_CAPITAL) * 100
        tp_gain_percent = (tp_gain_usd_abs / POSITION_CAPITAL) * 100
        
        sl_loss_usd = -sl_risk_usd_abs
        tp_gain_usd = tp_gain_usd_abs

    # 1. ヘッダーとエントリー情報の可視化
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好 (VERY HIGH)" if score_raw >= 0.85 else ("高 (HIGH)" if score_raw >= 0.75 else "中 (MEDIUM)")
    
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    market_sentiment_str = ""
    macro_sentiment = best_signal.get('macro_context', {}).get('sentiment_fgi_proxy', 0.0) 
    if macro_sentiment >= 0.4:
         market_sentiment_str = " (リスクオン傾向)"
    elif macro_sentiment <= -0.4:
         market_sentiment_str = " (リスクオフ傾向)"
    
    exit_type_str = "DTS (動的追跡損切)" 
    
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

    pnl_block = (
        f"\n**💵 損益予測 ({POSITION_CAPITAL:,.0f} USD ポジションの場合) [1x想定]**\n"
        f"----------------------------------\n"
        f"| 項目 | **損益額 (USD)** | 損益率 (対ポジションサイズ) |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ❌ SL実行時 | **{format_pnl_utility_telegram(sl_loss_usd)}** | {sl_risk_percent:.2f}% |\n" 
        f"| 🟢 TP目標時 | **{format_pnl_utility_telegram(tp_gain_usd)}** | {tp_gain_percent:.2f}% |\n"
        f"----------------------------------\n"
    )
    
    pivot_points = best_signal.get('tech_data', {}).get('pivot_points', {})
    
    pivot_pnl_block = ""
    pivot_r1 = pivot_points.get('r1', 0.0)
    pivot_r2 = pivot_points.get('r2', 0.0)
    pivot_s1 = pivot_points.get('s1', 0.0)
    pivot_s2 = pivot_points.get('s2', 0.0)

    if pivot_r1 > 0 and entry_price > 0 and side in ["ロング", "ショート"]:
        
        is_long = (side == "ロング")
        pnl_r1 = calculate_pnl_at_pivot(pivot_r1, entry_price, is_long, POSITION_CAPITAL)
        pnl_r2 = calculate_pnl_at_pivot(pivot_r2, entry_price, is_long, POSITION_CAPITAL)
        pnl_s1 = calculate_pnl_at_pivot(pivot_s1, entry_price, is_long, POSITION_CAPITAL)
        pnl_s2 = calculate_pnl_at_pivot(pivot_s2, entry_price, is_long, POSITION_CAPITAL)
        
        pivot_pnl_block = (
            f"\n**🧮 ${POSITION_CAPITAL:,.0f} ポジションの到達損益 (Pivot S/R) [1x想定]**\n"
            f"----------------------------------\n"
            f"| 目標レベル | **価格 (USD)** | 損益 (概算) |\n"
            f"| :--- | :--- | :--- |\n"
            f"| 📈 **抵抗線 R1** | <code>${format_price_utility(pivot_r1, symbol)}</code> | {format_pnl_utility_telegram(pnl_r1)} |\n"
            f"| 🚨 **抵抗線 R2** | <code>${format_price_utility(pivot_r2, symbol)}</code> | {format_pnl_utility_telegram(pnl_r2)} |\n"
            f"| 📉 **支持線 S1** | <code>${format_price_utility(pivot_s1, symbol)}</code> | {format_pnl_utility_telegram(pnl_s1)} |\n"
            f"| 🧊 **支持線 S2** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )

    # 2. テクニカル概要
    tech_data = best_signal.get('tech_data', {})
    
    consistency = best_signal.get('trend_consistency', {})
    consistency_str = "✅ 高い" if consistency.get('is_consistent') else "⚠️ 低い"
    
    macro_context = best_signal.get('macro_context', {})
    
    btc_trend = macro_context.get('btc_price_trend', 'Neutral')
    btc_trend_str = "⬆️ 強気トレンド" if btc_trend.startswith("Strong_Uptrend") else ("⬆️ トレンド" if btc_trend.startswith("Uptrend") else ("⬇️ 弱気トレンド" if btc_trend.startswith("Strong_Downtrend") else ("⬇️ トレンド" if btc_trend.startswith("Downtrend") else "中立")))
    
    fgi_proxy = macro_context.get('sentiment_fgi_proxy', 0.0)
    if fgi_proxy > 0.4:
        fgi_level = "Greed"
    elif fgi_proxy < -0.4:
        fgi_level = "Fear"
    else:
        fgi_level = "Neutral"

    trend_summary = (
        f"\n**💡 トレンドとボラティリティの概要**\n"
        f"----------------------------------\n"
        f"| 指標 | 値 | 評価 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| **ADX** | {tech_data.get('adx', 0.0):.2f} | トレンドの**強さ** ({tech_data.get('adx_strength', 'N/A')}) |\n"
        f"| **ATR** | {tech_data.get('atr_value', 0.0):.4f} | **平均的なボラティリティ** (過去14期間) |\n"
        f"| **RSI** | {tech_data.get('rsi', 0.0):.2f} | **買われ過ぎ/売られ過ぎ** ({tech_data.get('rsi_regime', 'N/A')}) |\n"
        f"| **MACD** | {tech_data.get('macd_signal', 'N/A')} | **勢い** ({tech_data.get('macd_momentum', 'N/A')}) |\n"
        f"| **トレンド一致** | {consistency_str} | **{consistency.get('match_count')}/3** の時間軸でトレンド一致 |\n"
        f"----------------------------------\n"
    )

    # 3. マクロ要因
    macro_summary = (
        f"\n**🌍 マクロコンテキストフィルター (v17.0.9)**\n"
        f"----------------------------------\n"
        f"| 要因 | 値 | 評価 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| **BTC Trend**| {btc_trend_str} | **BTC価格動向** (Altcoinバイアス) |\n"
        f"| **FR Bias** | {macro_context.get('funding_rate', 0.0) * 100:.4f}% | **資金調達率の偏り** ({macro_context.get('funding_rate_bias', 'NEUTRAL')}) |\n"
        f"| **FGI Proxy**| {fgi_proxy:.2f} | **市場心理** ({fgi_level}) |\n"
        f"----------------------------------\n"
    )
    
    # 最終的なメッセージ結合
    message = (
        f"{header}"
        f"{trade_plan}"
        f"{pnl_block}"
        f"{pivot_pnl_block}"
        f"{trend_summary}"
        f"{macro_summary}"
        f"--- ⚠️ --- **免責事項** --- ⚠️ ---\n"
        f"この情報は自動分析によるものです。最終的な取引判断はご自身の責任で行ってください。"
    )

    return message

# --- NEW: スコア不成立時の通知メッセージ整形関数 ---
def format_insufficient_analysis_message(signal: Dict, threshold: float) -> str:
    """
    スコアが閾値に満たなかった場合に送信するメッセージを整形する。(v17.0.10)
    """
    symbol = signal.get('symbol', 'N/A').replace('-', '/')
    timeframe = signal.get('timeframe', 'N/A')
    side = signal.get('side', 'N/A').upper()
    score_raw = signal.get('score', 0.5)
    score_100 = score_raw * 100
    
    # マクロコンテキスト
    macro_context = signal.get('macro_context', {})
    btc_trend = macro_context.get('btc_price_trend', 'Neutral')
    
    # トレンド文字列を簡略化 (Strong_Uptrend -> ⬆️ 強気トレンド)
    if btc_trend.startswith("Strong_Uptrend"):
        btc_trend_str = "⬆️ 強気トレンド"
    elif btc_trend.startswith("Strong_Downtrend"):
        btc_trend_str = "⬇️ 弱気トレンド"
    else:
        btc_trend_str = "中立/混合"
    
    rr_ratio = signal.get('rr_ratio', 0.0)

    message = (
        f"--- ⚠️ --- **シグナル不成立通知** --- ⚠️ ---\n"
        f"**🚨 現在、取引実行基準スコア ({threshold*100:.0f}点) を満たすシグナルはありませんでした。**\n"
        f"==================================\n"
        f"| 🏆 **優良候補 1 位** | **{symbol}** ({timeframe}足) |\n"
        f"| 📉 **方向性** | **{side}** |\n"
        f"| 💯 **最高スコア** | <b>{score_100:.2f} / 100.00 点</b> (閾値: {threshold*100:.0f}点) |\n"
        f"| 💰 **予想損益比** | **1:{rr_ratio:.2f}** (目安) |\n"
        f"| 🌍 **市場環境** | **BTCトレンド: {btc_trend_str}** |\n"
        f"==================================\n"
        f"この銘柄はスコアは高いものの、リスク管理上の理由から自動通知・取引は見送られました。"
    )
    return message
# -----------------------------------------------------

# ====================================================================================
# CCXT WRAPPERS
# ====================================================================================

async def initialize_ccxt_client() -> None:
    """CCXTクライアントを初期化または再初期化する"""
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
        except Exception as e:
            logging.warning(f"既存のCCXTクライアントのクローズ中にエラー: {e}")
            
    try:
        if CCXT_CLIENT_NAME == 'OKX':
            exchange_class = getattr(ccxt_async, 'okx')
        else:
            exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
            
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': os.environ.get(f'{CCXT_CLIENT_NAME}_API_KEY', 'YOUR_API_KEY'),
            'secret': os.environ.get(f'{CCXT_CLIENT_NAME}_SECRET', 'YOUR_SECRET'),
            'password': os.environ.get(f'{CCXT_CLIENT_NAME}_PASSWORD'), 
            'options': {
                'defaultType': 'future', 
            },
            'enableRateLimit': True,
        })
        logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で正常に初期化しました。")
        
    except Exception as e:
        logging.error(f"CCXTクライアントの初期化に失敗: {e}")
        EXCHANGE_CLIENT = None

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int = 500) -> Tuple[pd.DataFrame, bool]:
    """CCXTを使用してOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        await initialize_ccxt_client()
        if not EXCHANGE_CLIENT:
            logging.error(f"OHLCV取得失敗: {symbol} クライアントが利用できません。")
            return pd.DataFrame(), True 

    ccxt_symbol = symbol.replace('-', '/') 
    
    try:
        # OKXの場合、パーペチュアルスワップを指定
        if EXCHANGE_CLIENT.id == 'okx':
            ccxt_symbol += ':USDT-SWAP'
            
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"{symbol} のOHLCVデータを取得できませんでした。市場が存在しない可能性があります。")
            return pd.DataFrame(), True
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        return df, False 
        
    except ccxt.NetworkError as e:
        logging.error(f"CCXTネットワークエラー {symbol} {timeframe}: {e}")
        await asyncio.sleep(5)
        return pd.DataFrame(), False
    except ccxt.ExchangeError as e:
        # 例: Symbol not found, Invalid contract type
        logging.warning(f"CCXT取引所エラー {symbol} {timeframe}: {e}")
        return pd.DataFrame(), True
    except Exception as e:
        logging.error(f"その他のOHLCV取得エラー {symbol} {timeframe}: {e}")
        return pd.DataFrame(), False

# ====================================================================================
# MACRO CONTEXT FETCHING (v17.0.11 FIX APPLIED)
# ====================================================================================

def get_btc_dominance_context(df: pd.DataFrame) -> Tuple[float, str]:
    """
    BTC-USDの価格データから、トレンドバイアスと方向性を決定する。
    """
    # 既存のロジックを維持 (SMA計算はPandas標準機能を使用)
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH + SMA_LENGTH:
        return 0.0, "DataShortage"

    close = df['Close']
    
    # 20期間SMAを計算 (短期トレンド)
    sma_20 = close.rolling(window=SMA_LENGTH).mean()
    
    # 50期間SMAを計算 (長期トレンド)
    sma_50 = close.rolling(window=LONG_TERM_SMA_LENGTH).mean()
    
    latest_close = close.iloc[-1]
    latest_sma_20 = sma_20.iloc[-1]
    latest_sma_50 = sma_50.iloc[-1]
    
    # トレンド判定
    bias = 0.0
    trend_description = "Neutral"

    if latest_close > latest_sma_20 and latest_sma_20 > latest_sma_50:
        bias = 1.0
        trend_description = "Strong_Uptrend"
    elif latest_close < latest_sma_20 and latest_sma_20 < latest_sma_50:
        bias = -1.0
        trend_description = "Strong_Downtrend"
    elif latest_close > latest_sma_20:
        bias = 0.5
        trend_description = "Uptrend"
    elif latest_close < latest_sma_20:
        bias = -0.5
        trend_description = "Downtrend"
        
    logging.info(f"BTC価格トレンド: {trend_description} (最新終値: {latest_close:,.2f}, SMA_20: {latest_sma_20:,.2f})")
    
    return bias, trend_description

async def fetch_global_macro_context() -> Dict:
    """BTCのデータ、FR、センチメントを取得し、グローバルマクロコンテキストを構築する"""
    
    fr_proxy = 0.0 
    fr_bias = "NEUTRAL"
    sentiment_fgi_proxy = -0.10 
    
    # 1. BTC価格トレンド (代理としてBTC-USDの価格トレンドを使用)
    try:
        # YF.download()のauto_adjust引数の警告を抑制
        dom_df = yf.download("BTC-USD", period="7d", interval="4h", progress=False, auto_adjust=False)
        
        # --- v17.0.11 FIX: yfinanceのDataFrameがMultiIndexで返される場合の対策 ---
        # 1. 列のMultiIndexを平坦化 (通常は複数ティッカー時だが、環境依存で発生するため)
        if isinstance(dom_df.columns, pd.MultiIndex):
            dom_df.columns = [col[0] for col in dom_df.columns.values]
            
        # 2. 行のMultiIndexを平坦化 (エラーメッセージ "not MultiIndex" の直接的な原因対策)
        # droplevel(axis=0) は MultiIndexの場合のみ適用可能
        if isinstance(dom_df.index, pd.MultiIndex):
            # 最初のレベルのみを残す
            dom_df = dom_df.droplevel(level=0, axis=0)
        # ------------------------------------------------------------------------

        dom_df.ta.ema(length=20, append=True) # EMAを計算しておく (将来の使用のため)

        btc_bias, btc_trend = get_btc_dominance_context(dom_df)
    except Exception as e:
        error_name = type(e).__name__
        logging.error(f"BTCドミナンスデータの取得に失敗: {error_name} {e}")
        btc_bias, btc_trend = 0.0, "FetchFailed"

    # 2. Funding Rate (FR) - 今回はダミー値を使用
    # 実際のFRの取得は取引所APIまたは外部ソースが必要
    # fr_proxy = fetch_average_funding_rate() 
    
    if fr_proxy > FUNDING_RATE_THRESHOLD:
        fr_bias = "LONG_BIAS" # ロングポジションが優勢 (FRはプラス)
    elif fr_proxy < -FUNDING_RATE_THRESHOLD:
        fr_bias = "SHORT_BIAS" # ショートポジションが優勢 (FRはマイナス)

    # 3. Sentiment/FGI (Fear & Greed Index) - 今回はダミー値を使用
    # sentiment_fgi_proxy = fetch_fgi_data() 

    context = {
        'btc_price_bias': btc_bias,
        'btc_price_trend': btc_trend,
        'funding_rate': fr_proxy,
        'funding_rate_bias': fr_bias,
        'sentiment_fgi_proxy': sentiment_fgi_proxy
    }
    
    logging.info(f"グローバルマクロコンテキスト: Dom Bias={context['btc_price_bias']:.2f}, FR={context['funding_rate']:.4f}%, Sentiment={context['sentiment_fgi_proxy']:.2f}")
    
    return context

# ====================================================================================
# ANALYTICS & CORE LOGIC
# ====================================================================================

def calculate_pivot_points(df: pd.DataFrame) -> Dict[str, float]:
    """
    直近のPivotポイントを計算する (Classic Pivot Point)。
    """
    if df.empty:
        return {'pp': 0.0, 'r1': 0.0, 'r2': 0.0, 's1': 0.0, 's2': 0.0}
        
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]

    # 前日の高値・安値・終値
    h = prev_row['High'] # yfinanceの列名に合わせる
    l = prev_row['Low']
    c = prev_row['Close']
    
    # Pivot Point (PP)
    pp = (h + l + c) / 3
    
    # Resistance (R) and Support (S)
    r1 = (2 * pp) - l
    s1 = (2 * pp) - h
    r2 = pp + (h - l)
    s2 = pp - (h - l)
    
    return {'pp': pp, 'r1': r1, 'r2': r2, 's1': s1, 's2': s2}


def analyze_single_timeframe(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Dict:
    """
    単一の時間足のデータフレームに対して分析を実行し、シグナルをスコアリングする。
    """
    
    # 1. 指標の計算
    
    # ATR (Average True Range)
    df.ta.atr(length=ATR_LENGTH, append=True)
    atr_col = f'ATR_{ATR_LENGTH}'
    if atr_col not in df.columns:
        # pandas_taのバージョン依存で列名が変わる可能性を考慮
        atr_col = df.columns[df.columns.str.startswith('ATR')][-1] 
    
    # RSI (Relative Strength Index)
    df.ta.rsi(length=14, append=True)
    rsi_col = 'RSI_14'
    
    # MACD (Moving Average Convergence Divergence)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    macd_cols = [c for c in df.columns if c.startswith('MACD_')]
    macd_line_col = macd_cols[0] 
    macd_signal_col = macd_cols[1]

    # ADX (Average Directional Index)
    df.ta.adx(length=14, append=True)
    adx_col = 'ADX_14'

    # SMA (Simple Moving Average) - 長期トレンド判定用
    df['SMA_LONG'] = df['close'].rolling(window=LONG_TERM_SMA_LENGTH).mean()
    
    # Pivot Points
    pivot_points = calculate_pivot_points(df)

    # 2. 最新データの取得と準備
    last_row = df.iloc[-1]
    price = last_row['close']
    last_atr = last_row[atr_col]
    last_rsi = last_row[rsi_col]
    last_macd_line = last_row[macd_line_col]
    last_macd_signal = last_row[macd_signal_col]
    last_adx = last_row[adx_col]
    last_sma_long = last_row['SMA_LONG']

    # 3. スコアリングの実行
    score = BASE_SCORE # 初期スコア 0.40
    side = "Neutral"
    
    # RSIレジーム判定
    rsi_regime = "Neutral"
    if last_rsi < RSI_OVERSOLD: rsi_regime = "Oversold"
    elif last_rsi > RSI_OVERBOUGHT: rsi_regime = "Overbought"
    elif last_rsi < RSI_MOMENTUM_LOW: rsi_regime = "Weak-Bullish"
    elif last_rsi > RSI_MOMENTUM_HIGH: rsi_regime = "Strong-Bullish"

    # ADX強度判定
    adx_strength = "Weak"
    if last_adx > ADX_TREND_THRESHOLD: adx_strength = "Strong"
    elif last_adx > 20: adx_strength = "Medium"

    # MACDクロス判定
    macd_signal = "Neutral"
    if last_macd_line > last_macd_signal and df[macd_line_col].iloc[-2] <= df[macd_signal_col].iloc[-2]:
        macd_signal = "Bullish Crossover"
    elif last_macd_line < last_macd_signal and df[macd_line_col].iloc[-2] >= df[macd_signal_col].iloc[-2]:
        macd_signal = "Bearish Crossover"

    # 4. トレンド方向性の決定とスコアの加算/減算

    if price > last_sma_long:
        # LONGバイアス (長期上昇トレンド)
        side = "ロング"
        
        # 4-1. MACD 強気クロス
        if macd_signal == "Bullish Crossover":
            score += 0.20
        
        # 4-2. RSI 買われ過ぎ/モメンタム
        if rsi_regime == "Oversold" or rsi_regime == "Weak-Bullish": 
            score += 0.15 # 押し目買いのチャンス
        elif rsi_regime == "Overbought":
            score -= 0.10 # 買われ過ぎペナルティ
        
        # 4-3. ADX 強さ
        if adx_strength == "Strong":
            score += 0.05
        
        # 4-4. MACDがデッドクロス (長期トレンドとの逆行)
        if macd_signal == "Bearish Crossover":
            score -= MACD_CROSS_PENALTY # ペナルティ

    elif price < last_sma_long:
        # SHORTバイアス (長期下降トレンド)
        side = "ショート"
        
        # 4-1. MACD 弱気クロス
        if macd_signal == "Bearish Crossover":
            score += 0.20
        
        # 4-2. RSI 売られ過ぎ/モメンタム
        if rsi_regime == "Overbought" or rsi_regime == "Strong-Bullish":
            score += 0.15 # 戻り売りのチャンス
        elif rsi_regime == "Oversold":
            score -= 0.10 # 売られ過ぎペナルティ
            
        # 4-3. ADX 強さ
        if adx_strength == "Strong":
            score += 0.05

        # 4-4. MACDがゴールデンクロス (長期トレンドとの逆行)
        if macd_signal == "Bullish Crossover":
            score -= MACD_CROSS_PENALTY # ペナルティ

    # 5. 価格アクションによる調整 (例: 大陽線/大陰線後の反転狙い)
    # last_candle_change = (last_row['close'] - last_row['open']) / last_row['open']
    # if abs(last_candle_change) > 0.005: 
    #     score += 0.05 * (-1 if last_candle_change > 0 else 1) * (1 if side == "ショート" else -1) # トレンド反転狙いのボーナス

    # 6. マクロコンテキストフィルターの適用 (v17.0.8)
    macro_score_adjustment = 0.0
    
    # 6-1. BTC価格トレンドフィルター (Altcoinバイアス)
    btc_bias = macro_context.get('btc_price_bias', 0.0)
    if btc_bias > 0.0 and side == "ロング":
        macro_score_adjustment += DOMINANCE_BIAS_BONUS_PENALTY
    elif btc_bias < 0.0 and side == "ショート":
        macro_score_adjustment += DOMINANCE_BIAS_BONUS_PENALTY
    else:
        macro_score_adjustment -= DOMINANCE_BIAS_BONUS_PENALTY # 逆行方向の場合はペナルティ
        
    # 6-2. Funding Rate Bias
    fr_proxy = macro_context.get('funding_rate', 0.0)
    fr_bias = macro_context.get('funding_rate_bias', 'NEUTRAL')
    if fr_bias == 'LONG_BIAS' and side == "ショート": 
        macro_score_adjustment += FUNDING_RATE_BONUS_PENALTY # 逆張りのFRはボーナス
    elif fr_bias == 'SHORT_BIAS' and side == "ロング":
        macro_score_adjustment += FUNDING_RATE_BONUS_PENALTY 
    
    score = min(1.0, max(0.0, score + macro_score_adjustment))


    # 7. エントリー、SL、TPの計算
    
    entry_type = "Buy Limit (PP or S1/R1)"
    entry_price = price 
    sl_price = 0.0
    dynamic_tp_price = 0.0
    structural_sl_used = False
    
    atr_risk = last_atr * ATR_TRAIL_MULTIPLIER
    
    if side == "ロング":
        # エントリー: 構造的サポートS1を狙う (Limit注文)
        entry_price = pivot_points['s1'] 
        entry_type = "Buy Limit (S1付近)"
        
        # SL: ATRに基づくSL または S2を使用
        sl_atr = entry_price - atr_risk
        
        # 構造的SL S2
        if pivot_points['s2'] > 0 and pivot_points['s2'] < sl_atr:
             # S2の方がタイトなSLならS2を採用 + 0.5 ATRバッファ (v17.0.8 FIX)
             sl_price = pivot_points['s2'] - (last_atr * 0.5) 
             structural_sl_used = True
        else:
             sl_price = sl_atr
        
        # TP: Dynamic ATR multiplier
        tp_multiplier = TP_ATR_MULTIPLIERS.get(timeframe, ATR_TRAIL_MULTIPLIER)
        dynamic_tp_price = entry_price + (last_atr * tp_multiplier)

    elif side == "ショート":
        # エントリー: 構造的レジスタンスR1を狙う (Limit注文)
        entry_price = pivot_points['r1'] 
        entry_type = "Sell Limit (R1付近)"
        
        # SL: ATRに基づくSL または R2を使用
        sl_atr = entry_price + atr_risk
        
        # 構造的SL R2
        if pivot_points['r2'] > 0 and pivot_points['r2'] > sl_atr:
            # R2の方がタイトなSLならR2を採用 + 0.5 ATRバッファ (v17.0.8 FIX)
            sl_price = pivot_points['r2'] + (last_atr * 0.5) 
            structural_sl_used = True
        else:
            sl_price = sl_atr

        # TP: Dynamic ATR multiplier
        tp_multiplier = TP_ATR_MULTIPLIERS.get(timeframe, ATR_TRAIL_MULTIPLIER)
        dynamic_tp_price = entry_price - (last_atr * tp_multiplier)

    # 8. RRRの計算
    rr_ratio = 0.0
    if entry_price > 0 and sl_price > 0 and dynamic_tp_price > 0:
        risk = abs(entry_price - sl_price)
        reward = abs(dynamic_tp_price - entry_price)
        if risk > 0.0001:
            rr_ratio = reward / risk
        
    # 9. 最終結果の構築
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'price': price,
        'side': side,
        'score': score,
        'rr_ratio': rr_ratio,
        'entry': entry_price,
        'sl': sl_price,
        'dynamic_tp_price': dynamic_tp_price,
        'entry_type': entry_type,
        'tp_multiplier_used': tp_multiplier,
        'macro_context': macro_context,
        'tech_data': {
            'atr_value': last_atr,
            'adx': last_adx,
            'adx_strength': adx_strength,
            'rsi': last_rsi,
            'rsi_regime': rsi_regime,
            'macd_signal': macd_signal,
            'macd_momentum': "Long" if last_macd_line > last_macd_signal else "Short",
            'structural_sl_used': structural_sl_used,
            'pivot_points': pivot_points
        }
    }
    
    return result

async def run_technical_analysis(symbol: str) -> Tuple[List[Dict], bool]: 
    """
    指定されたシンボルに対して複数の時間足で分析を実行し、結果を統合する。
    
    戻り値: (分析結果リスト, is_market_missing: bool)
    """
    timeframes = ['15m', '1h', '4h']
    all_results = []
    
    is_market_missing = False 
    
    for tf in timeframes:
        df, is_market_missing_tf = await fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf]) 
        
        if is_market_missing_tf:
            is_market_missing = True 
            break 
            
        if not df.empty and len(df) >= REQUIRED_OHLCV_LIMITS[tf]:
            try:
                result = analyze_single_timeframe(df, symbol, tf, GLOBAL_MACRO_CONTEXT)
                all_results.append(result)
            except Exception as e:
                error_name = type(e).__name__
                logging.error(f"分析エラー {symbol} {tf}: {error_name} {e}")
                all_results.append({
                    'symbol': symbol, 'timeframe': tf, 'side': 'ExchangeError', 
                    'score': 0.50, 'rr_ratio': 0.0, 'macro_context': GLOBAL_MACRO_CONTEXT
                })
        else:
             all_results.append({
                'symbol': symbol, 'timeframe': tf, 'side': 'DataShortage', 
                'score': 0.50, 'rr_ratio': 0.0, 'macro_context': GLOBAL_MACRO_CONTEXT
             })

        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

    
    if not is_market_missing:
        valid_signals = [r for r in all_results if r['side'] in ['ロング', 'ショート']]
        
        if valid_signals:
            long_count = sum(1 for r in valid_signals if r['side'] == 'ロング')
            short_count = sum(1 for r in valid_signals if r['side'] == 'ショート')
            
            is_consistent = (long_count == 3 or short_count == 3)
            match_count = max(long_count, short_count)
            
            consistency_data = {
                'is_consistent': is_consistent,
                'match_count': match_count,
                'long_count': long_count,
                'short_count': short_count
            }

            best_signal_index = -1
            best_score = -1.0
            
            for i, r in enumerate(all_results):
                if r['side'] in ['ロング', 'ショート']:
                    if r['score'] > best_score:
                        best_score = r['score']
                        best_signal_index = i
                    
                    r['trend_consistency'] = consistency_data
                    
            # トレンド一致ボーナスの適用
            if is_consistent and best_signal_index != -1:
                all_results[best_signal_index]['score'] = min(1.0, all_results[best_signal_index]['score'] + TREND_CONSISTENCY_BONUS)
            

    return all_results, is_market_missing 


# ====================================================================================
# MAIN EXECUTION LOOP
# ====================================================================================

async def main_loop():
    """ボットのメイン実行ループ"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, TRADE_NOTIFIED_SYMBOLS, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, LAST_SUCCESSFUL_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT

    await initialize_ccxt_client()

    while True:
        try:
            current_time_j = datetime.now(JST)
            logging.info(f"🔄 Apex BOT v17.0.11 実行開始: {current_time_j.strftime('%Y-%m-%d %H:%M:%S JST')}") # バージョン更新

            logging.info(f"監視対象シンボル: {len(CURRENT_MONITOR_SYMBOLS)} 種類を決定しました。")

            # 2. グローバルマクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await fetch_global_macro_context()

            # 3. テクニカル分析の実行
            all_analysis_results = []
            symbols_to_remove = set() 
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                results, is_market_missing = await run_technical_analysis(symbol)
                
                if is_market_missing: 
                    symbols_to_remove.add(symbol)
                    
                all_analysis_results.extend(results)
                
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL * 2.0) 
            
            LAST_ANALYSIS_SIGNALS = all_analysis_results
            
            # 4. 監視対象シンボルの更新 (致命的エラーが発生したシンボルを除外)
            if symbols_to_remove:
                CURRENT_MONITOR_SYMBOLS = [
                    s for s in CURRENT_MONITOR_SYMBOLS 
                    if s not in symbols_to_remove
                ]
                logging.warning(f"以下のシンボルは取引所に存在しないため、監視リストから除外しました: {list(symbols_to_remove)}")
                TRADE_NOTIFIED_SYMBOLS = {
                    k: v for k, v in TRADE_NOTIFIED_SYMBOLS.items() 
                    if k not in symbols_to_remove
                }

            
            # 5. シグナルのフィルタリングと統合
            
            # 有効なシグナル (ロング/ショート) を抽出 (閾値未満も含む)
            all_trade_signals_pre_filter = [
                s for s in LAST_ANALYSIS_SIGNALS 
                if s['side'] in ['ロング', 'ショート'] 
                and s['score'] > 0.0 # スコアが0より大きい有効なシグナル
            ]

            # シンボルごとに最もスコアの高いシグナルを採用 (3つの時間足からベストスコアを選択)
            best_signals_per_symbol: Dict[str, List[Dict]] = {}
            for s in all_trade_signals_pre_filter:
                symbol = s['symbol']
                # シグナルリストの中で最もスコアの高いものを選択
                if symbol not in best_signals_per_symbol or s['score'] > best_signals_per_symbol[symbol][0]['score']:
                     best_signals_per_symbol[symbol] = [s]
                
            # 統合されたシグナルの最終選定 (シンボルごとのベストスコアを持つ単一シグナル)
            integrated_signals = [s[0] for s in best_signals_per_symbol.values()]
                
            # スコアとRRRに基づいて最終ソート (全シグナルを対象)
            all_sorted_signals = sorted(
                integrated_signals, 
                key=lambda s: (s['score'], s.get('rr_ratio', 0.0)), 
                reverse=True
            )
            
            # 閾値を超えるシグナルをフィルタリング
            final_trade_signals = [s for s in all_sorted_signals if s['score'] >= SIGNAL_THRESHOLD]

            # 6. 通知ロジック
            
            # クールダウン期間の経過チェック
            now = time.time()
            TRADE_NOTIFIED_SYMBOLS = {
                k: v for k, v in TRADE_NOTIFIED_SYMBOLS.items() 
                if now - v < TRADE_SIGNAL_COOLDOWN
            }

            notification_count = 0
            
            # --- 【A: 閾値達成シグナルの通知】 ---
            for rank, signal in enumerate(final_trade_signals[:TOP_SIGNAL_COUNT]):
                symbol = signal['symbol']
                
                # クールダウンチェック
                if symbol in TRADE_NOTIFIED_SYMBOLS:
                    continue
                
                # 統合分析メッセージを整形
                full_signals_for_symbol = [s for s in all_analysis_results if s['symbol'] == symbol]
                message = format_integrated_analysis_message(symbol, full_signals_for_symbol, rank + 1)
                
                if message:
                    send_telegram_html(message)
                    TRADE_NOTIFIED_SYMBOLS[symbol] = now
                    notification_count += 1
            
            # --- 【B: 閾値不成立だが最高のシグナルの通知 (要望対応)】 ---
            # 閾値達成シグナルがゼロの場合のみ実行
            if notification_count == 0 and all_sorted_signals:
                best_signal_overall = all_sorted_signals[0]
                best_symbol_overall = best_signal_overall['symbol']
                
                # スコアが閾値未満であることを確認
                if best_signal_overall['score'] < SIGNAL_THRESHOLD:
                    # 最もスコアが高い銘柄がクールダウン中でなければ通知
                    if best_symbol_overall not in TRADE_NOTIFIED_SYMBOLS:
                        # 閾値未満であることを示す特別なメッセージを生成
                        insufficient_message = format_insufficient_analysis_message(best_signal_overall, SIGNAL_THRESHOLD)
                        send_telegram_html(insufficient_message)
                        TRADE_NOTIFIED_SYMBOLS[best_symbol_overall] = now # クールダウンに追加 (再通知を避けるため)
                        notification_count += 1
                        logging.info(f"Telegram通知を 1 件送信しました。(スコア不成立: {best_symbol_overall.replace('-', '/')})")
                
            
            LAST_UPDATE_TIME = now
            LAST_SUCCESS_TIME = now
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = CURRENT_MONITOR_SYMBOLS.copy()
            logging.info("====================================")
            logging.info(f"✅ Apex BOT v17.0.11 実行完了。次の実行まで {LOOP_INTERVAL} 秒待機します。") # バージョン更新
            logging.info(f"通知クールダウン中のシンボル: {list(TRADE_NOTIFIED_SYMBOLS.keys())}")
            
            await asyncio.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            logging.info("ユーザーにより中断されました。シャットダウンします。")
            break
        except Exception as e:
            error_name = type(e).__name__
            if 'ccxt' in error_name.lower():
                 logging.warning("CCXT関連のエラーを検知。クライアントを再初期化します...")
                 await initialize_ccxt_client()
            
            logging.error(f"メインループで致命的なエラー: {error_name} {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.11 - FIX: BTC Dominance MultiIndex Error") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.11 Startup initializing...") # バージョン更新
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
        "bot_version": "v17.0.11 - FIX: BTC Dominance MultiIndex Error", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.11)"}) # バージョン更新

# ====================================================================================
# EXECUTION (If run directly)
# ====================================================================================

if __name__ == "__main__":
    # uvicorn.run("main_render:app", host="0.0.0.0", port=10000)
    pass
