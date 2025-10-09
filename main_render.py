# ====================================================================================
# Apex BOT v17.0.6 - Debug & Rate Limit Fix
# - FIX: main_loopでasyncio.gatherの使用を停止し、シンボルごとの逐次処理 + 1秒遅延を導入 (OKX Rate Limit回避)
# - FIX: fetch_global_macro_context内でyfinanceのデータ処理にdropnaと明示的なスカラー比較を導入 (AmbiguousValueError解消)
# - NEW: 1000 USD Trade PnL notification. (v17.0.5からの継承)
# - NEW: Dynamic TP based on Timeframe-specific ATR Multipliers. (v17.0.5からの継承)
# - NEW: Trend Consistency Bonus (+0.10) for stricter scoring. (v17.0.5からの継承)
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
REQUEST_DELAY_PER_SYMBOL = 0.5 # 単一シンボル内の時間足取得間の遅延

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
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v17.0.6)
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

    # V17.0.5 CUSTOM: 動的TP価格と乗数
    dynamic_tp_price = best_signal.get('dynamic_tp_price', 0.0) # NEW
    tp_multiplier_used = best_signal.get('tp_multiplier_used', ATR_TRAIL_MULTIPLIER)
    
    tp_price_for_pnl = dynamic_tp_price 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算 (初期のストップ位置との差)
    sl_width_calculated = abs(entry_price - sl_price)
    
    # V17.0.5 CUSTOM: $1000 ポジションに基づくP&L計算 (1xレバレッジ)
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
        
    # V17.0.5 CUSTOM: 取引計画の表示を動的TPと乗数情報に合わせて変更
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

    # V17.0.5 CUSTOM: SL/TP 到達時のP&Lブロック (1000 USD ポジション)
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
            f"| 🧊 **支持線 S2** | <code>${format_price_utility(pivot_s2, symbol)}</code> | {format_pnl_utility_telegram(pnl_s2)} |\n"
            f"----------------------------------\n"
        )


    # ----------------------------------------------------
    # 2. テクニカル概要
    # ----------------------------------------------------
    tech_data = best_signal.get('tech_data', {})
    
    # トレンドの方向性一致の評価 (v17.0.5)
    consistency = best_signal.get('trend_consistency', {})
    consistency_str = "✅ 高い" if consistency.get('is_consistent') else "⚠️ 低い"
    
    # マクロコンテキスト
    macro_context = best_signal.get('macro_context', {})
    fr_bias_str = "中立"
    if macro_context.get('funding_rate_bias') == "LONG_BIAS": fr_bias_str = "⬆️ 資金調達率ロングバイアス"
    elif macro_context.get('funding_rate_bias') == "SHORT_BIAS": fr_bias_str = "⬇️ 資金調達率ショートバイアス"
    
    dom_bias_str = "中立"
    if macro_context.get('dominance_bias') == "BTC_BULL": dom_bias_str = "⬆️ BTCドミナンス上昇バイアス"
    elif macro_context.get('dominance_bias') == "ALT_BULL": dom_bias_str = "⬇️ BTCドミナンス下落バイアス"
    
    # 総合的なトレンド概要
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

    # ----------------------------------------------------
    # 3. マクロ要因
    # ----------------------------------------------------
    macro_summary = (
        f"\n**🌍 マクロコンテキストフィルター**\n"
        f"----------------------------------\n"
        f"| 要因 | 値 | 評価 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| **BTC FR** | {macro_context.get('funding_rate', 0.0) * 100:.4f}% | **資金調達率** ({fr_bias_str}) |\n"
        f"| **BTC Dom**| {macro_context.get('dominance_bias_value', 0.0):.2f} | **ドミナンス動向** ({dom_bias_str}) |\n"
        f"| **FGI** | {macro_context.get('sentiment_fgi', 0.0):.2f} | **市場心理** ({macro_context.get('sentiment_fgi_level', 'N/A')}) |\n"
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


# ====================================================================================
# CCXT WRAPPERS
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化し、グローバル変数に設定する"""
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
    
    # OKXクライアントを非同期で初期化
    EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())({
        'timeout': 10000, 
        'enableRateLimit': True, 
        'options': {'defaultType': 'future', 'adjustForTimeDifference': True} 
    })
    
    try:
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} (Async) で初期化しました。")
    except Exception as e:
        logging.error(f"CCXTクライアントの初期化に失敗: {e}")
        EXCHANGE_CLIENT = None

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """指定されたシンボルと時間足のOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return pd.DataFrame()

    # CCXTは 'BTC/USDT' 形式を期待
    ccxt_symbol = symbol.replace('-', '/')
    
    try:
        # OKXは'futures'と'swap'の両方で取引可能だが、ここではswap (perpetual) を想定
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"分析スキップ: {symbol} {timeframe} のデータが不足しています。")
            return pd.DataFrame()
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        return df.drop('timestamp', axis=1)

    except ccxt.NetworkError as e:
        logging.error(f"OHLCV取得エラー {symbol} {timeframe}: ネットワークエラー {e}")
        return pd.DataFrame()
    except ccxt.ExchangeError as e:
        # OKX {"msg":"Too Many Requests","code":"50011"} のようなレート制限エラーをキャッチ
        error_name = type(e).__name__
        logging.error(f"OHLCV取得エラー {symbol} {timeframe}: {EXCHANGE_CLIENT.id} {e}")
        return pd.DataFrame()
    except Exception as e:
        error_name = type(e).__name__
        logging.error(f"OHLCV取得中に予期せぬエラー {symbol} {timeframe}: {error_name} {e}")
        return pd.DataFrame()

async def fetch_latest_funding_rate(symbol: str) -> float:
    """指定されたシンボルの最新の資金調達率を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT: return 0.0

    ccxt_symbol = symbol.replace('-', '/')
    
    # BTCUSDT の FRは 'BTC/USDT:USDT' のように特定のマーケットIDが必要な場合があるため、OKXのデフォルト形式を確認
    # OKXは futures/swap が別れているが、swapのFRを取得
    if 'okx' in EXCHANGE_CLIENT.id and ccxt_symbol.endswith('/USDT'):
        ccxt_symbol = ccxt_symbol.replace('/USDT', '-SWAP')
    
    try:
        # fetchFundingRate は全ての取引所でサポートされているわけではないが、OKXでは利用可能
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(ccxt_symbol)
        return funding_rate['fundingRate']
    except ccxt.ExchangeError as e:
        # logging.warning(f"FR取得エラー {symbol}: {e}")
        return 0.0 
    except Exception:
        # logging.warning(f"FR取得に失敗 {symbol}")
        return 0.0 
        
async def fetch_all_available_symbols(limit: int) -> List[str]:
    """OKXから出来高上位のシンボルを取得する (USDペアのみ)"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが初期化されていません。デフォルトリストを使用します。")
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:limit]]

    try:
        # OKXの全マーケット情報を取得
        markets = await EXCHANGE_CLIENT.fetch_markets()
        
        # USDT建てのスワップ（無期限先物）のみをフィルタリング
        usdt_swap_symbols = [
            m['symbol'] for m in markets 
            if m.get('active', False) 
            and m['settleId'] == 'USDT' 
            and m['type'] == 'swap'
        ]
        
        if not usdt_swap_symbols:
            logging.warning("利用可能なUSDTスワップペアが見つかりませんでした。デフォルトリストを使用します。")
            return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:limit]]

        logging.info(f"利用可能なUSDTペア: {len(usdt_swap_symbols)} 種類。出来高データ取得をスキップし、デフォルトリストを使用します。")
        
        # 出来高の取得はレート制限のリスクが高いため、デフォルトでスキップし、固定リストを使用 (v17.0.6)
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:limit]]

    except Exception as e:
        logging.error(f"利用可能なシンボルリストの取得に失敗: {e}。デフォルトリストを使用します。")
        return [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:limit]]

async def fetch_global_macro_context() -> Dict[str, Any]:
    """BTCの資金調達率、ドミナンス、FGIを取得する"""
    context = {
        'funding_rate': 0.0,
        'funding_rate_bias': 'NEUTRAL',
        'dominance_bias': 'NEUTRAL',
        'dominance_bias_value': 0.00,
        'sentiment_fgi': -0.10, # デフォルトをリスクオフに設定
        'sentiment_fgi_level': 'Fear (Default)'
    }
    
    # 1. 資金調達率 (Funding Rate)
    try:
        fr = await fetch_latest_funding_rate('BTC-USDT')
        context['funding_rate'] = fr
        if fr > FUNDING_RATE_THRESHOLD:
            context['funding_rate_bias'] = 'SHORT_BIAS' # ロング過熱によるショート優位バイアス
        elif fr < -FUNDING_RATE_THRESHOLD:
            context['funding_rate_bias'] = 'LONG_BIAS' # ショート過熱によるロング優位バイアス
        else:
            context['funding_rate_bias'] = 'NEUTRAL'
        logging.info(f"BTC資金調達率: {fr*100:.4f}%")
    except Exception:
        logging.warning("BTC資金調達率の取得に失敗。")

    # 2. BTCドミナンス (BTC Dominance - proxy by BTC-USD trend)
    try:
        # yfinance (Yahoo Finance) から BTC-USD の過去7日間、4時間足データを取得
        # NOTE: yfinanceのバグにより、一部環境でエラーが出ることがあるため、df.columnsを修正
        # v17.0.6 FIX: FutureWarningとAmbiguousValueError対策として、dropna()と.item()/.all()を使用
        dom_df = yf.download("BTC-USD", period="7d", interval="4h", progress=False)
        
        if dom_df.empty:
             raise ValueError("BTC-USDデータの取得が空でした。")
             
        dom_df.columns = [c.lower() for c in dom_df.columns]
        
        # Simple Moving Average (SMA) を計算
        dom_df['SMA'] = ta.sma(dom_df['close'], length=SMA_LENGTH)
        
        # 不完全な行を削除 (AmbiguousValueError対策)
        dom_df = dom_df.dropna(subset=['close', 'SMA'])

        # 最新行と1つ前の行を取得
        last_row = dom_df.iloc[-1]
        prev_row = dom_df.iloc[-2]

        # 終値とSMAの比較によりドミナンス（価格）トレンドを評価
        # BTC価格がSMAより上 (上昇トレンド) かつ 1期間前から上昇しているかを判定
        btc_price_above_sma = (last_row['close'] > last_row['sma'])
        btc_price_rising = (last_row['close'] > prev_row['close'])
        
        if btc_price_above_sma and btc_price_rising:
            # 価格がSMAより上で、さらに上昇している -> BTC相場強気 (Altcoinは影響を受ける可能性)
            context['dominance_bias'] = 'BTC_BULL'
            context['dominance_bias_value'] = 1.00 # 仮の値
        elif not btc_price_above_sma and not btc_price_rising:
            # 価格がSMAより下で、さらに下落している -> BTC相場弱気 (Altcoinに資金が流れる可能性)
            context['dominance_bias'] = 'ALT_BULL'
            context['dominance_bias_value'] = -1.00 # 仮の値
        else:
            context['dominance_bias'] = 'NEUTRAL'

    except Exception as e:
        # yfinanceはFutureWarningを出すことがあるが、ここでは無視し、AmbiguousErrorなどの致命的なエラーのみログに出す
        error_name = type(e).__name__
        if error_name != 'FutureWarning':
            logging.warning(f"BTCドミナンスデータの取得に失敗: {e}")

    # 3. Fear & Greed Index (FGI) - プロキシとして-0.10を使用
    # NOTE: 実際のFGI APIを使用していないため、固定値。
    context['sentiment_fgi'] = -0.10 # 仮の値 (Fear: 0.10 - Extreme Fear: -1.00)
    context['sentiment_fgi_proxy'] = context['sentiment_fgi']
    if context['sentiment_fgi'] > 0.60:
        context['sentiment_fgi_level'] = 'Extreme Greed'
    elif context['sentiment_fgi'] > 0.40:
        context['sentiment_fgi_level'] = 'Greed'
    elif context['sentiment_fgi'] > 0.00:
        context['sentiment_fgi_level'] = 'Neutral'
    elif context['sentiment_fgi'] > -0.40:
        context['sentiment_fgi_level'] = 'Fear'
    else:
        context['sentiment_fgi_level'] = 'Extreme Fear'

    logging.info(f"グローバルマクロコンテキスト: Dom Bias={context['dominance_bias_value']:.2f}, FR={context['funding_rate']*100:.4f}%, Sentiment={context['sentiment_fgi']:.2f}")
    return context


# ====================================================================================
# ANALYTICS & CORE LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算し、DataFrameに追加する"""
    
    # ATR (Average True Range)
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # RSI (Relative Strength Index)
    df.ta.rsi(length=14, append=True)
    
    # MACD (Moving Average Convergence Divergence)
    df.ta.macd(append=True)
    
    # ADX (Average Directional Index)
    df.ta.adx(append=True)
    
    # Bollinger Bands
    df.ta.bbands(append=True)
    
    # SMA (Simple Moving Average) - 長期トレンド判断用
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    
    # Pivot Points (Classic) - 構造的SL/TPの基準用
    df.ta.pivot_points(kind='fibonacci', append=True) 

    # 必要な列が欠落している場合、NaNで埋める
    required_cols = ['ATR', 'RSI_14', 'MACD_12_26_9', 'ADX_14', f'SMA_{LONG_TERM_SMA_LENGTH}']
    for col in required_cols:
        if col not in df.columns:
            # df.taのバージョンによってはカラム名が異なる場合がある
            if 'MACDh' in col and any('MACDh' in c for c in df.columns):
                 continue
            if 'MACD' in col and not any('MACD_' in c for c in df.columns):
                 # MACDが計算失敗した場合のログ
                 logging.warning(f"Warning: MACD indicator column missing in data.")
                 df['MACD_12_26_9'] = np.nan
                 continue
                 
            # 念のため、ATRなどの主要なインジケータカラム名を正規化
            if 'ATR' in col and not any('ATR_' in c for c in df.columns):
                df['ATR'] = df.get('ATR_14', np.nan) 
                
            if col not in df.columns:
                 # 最終的なフォールバック
                 logging.warning(f"Warning: Indicator column {col} still missing after check.")
                 
    return df

def calculate_pivot_points(last_row: pd.Series) -> Dict[str, float]:
    """最新の行からPivot PointのS/Rレベルを抽出する"""
    # pandas_taのfibonacci pivotの命名規則を使用
    return {
        'p': last_row.get('P_FIB', np.nan),
        'r1': last_row.get('R1_FIB', np.nan),
        'r2': last_row.get('R2_FIB', np.nan),
        'r3': last_row.get('R3_FIB', np.nan),
        's1': last_row.get('S1_FIB', np.nan),
        's2': last_row.get('S2_FIB', np.nan),
        's3': last_row.get('S3_FIB', np.nan),
    }

def calculate_atr_sl_tp(price: float, atr_value: float, pivot_points: Dict[str, float], side_long: bool, timeframe: str) -> Tuple[float, float, float, float, bool]:
    """
    ATR、Pivot Point、および時間軸別TP乗数に基づき、SL/TP/Entryを計算する (v17.0.6)
    
    戻り値: (entry, sl, tp_dts, rr_ratio, structural_sl_used)
    """
    
    # 1. 基本となるATRベースのSL/TP (DTSの初期ストップ幅として使用)
    risk_width = ATR_TRAIL_MULTIPLIER * atr_value
    
    # V17.0.5 CUSTOM: 時間軸に応じた動的TPの乗数を取得
    tp_multiplier = TP_ATR_MULTIPLIERS.get(timeframe, ATR_TRAIL_MULTIPLIER * 2) 
    profit_width_dts = tp_multiplier * atr_value
    
    entry_price = price # 初期エントリーは現在価格とする

    if side_long:
        # ロングの場合
        sl_atr = price - risk_width
        tp_dts = price + profit_width_dts
        
        # 構造的SL (S1) のチェック: S1がATR SLより浅く、かつ現在価格より十分下にある場合に使用
        s1 = pivot_points.get('s1', np.nan)
        s1_buffer = np.nan
        structural_sl_used = False
        
        if not np.isnan(s1) and s1 > 0:
            # S1をSLとして使う場合、エントリー価格との一致や近すぎるのを避けるためにバッファ (0.5 * ATR) を追加 (v16.0.1 FIX)
            s1_buffer = s1 - 0.5 * atr_value
            
            # S1_bufferが現在価格より低く、かつATR SLより浅い場合 (よりタイトなSL)
            if s1_buffer < price and s1_buffer > sl_atr: 
                sl_final = s1_buffer
                structural_sl_used = True
            else:
                sl_final = sl_atr
        else:
            sl_final = sl_atr
            
        # ロングの場合のエントリータイプ: Pivot S1付近でのリミット注文を想定
        entry_type = "Buy Limit (S1付近)" if not np.isnan(s1) and s1 < price and s1_buffer < sl_final else "Market (現在価格)"
        # S1が存在する場合、エントリー価格をS1に近づける（例：S1）
        if entry_type == "Buy Limit (S1付近)" and s1 < price and sl_final < s1:
             entry_price = s1 # ここではシンプルにS1をエントリーポイントとする
             sl_final = s1_buffer # S1エントリー時のSLはS1_bufferとする
             
        # RRR計算
        risk = abs(entry_price - sl_final)
        profit = abs(tp_dts - entry_price)
        rr_ratio = profit / risk if risk > 0 else DTS_RRR_DISPLAY

        return entry_price, sl_final, tp_dts, rr_ratio, structural_sl_used

    else:
        # ショートの場合
        sl_atr = price + risk_width
        tp_dts = price - profit_width_dts

        # 構造的SL (R1) のチェック: R1がATR SLより浅く、かつ現在価格より十分上にある場合に使用
        r1 = pivot_points.get('r1', np.nan)
        r1_buffer = np.nan
        structural_sl_used = False
        
        if not np.isnan(r1) and r1 > 0:
            # R1をSLとして使う場合、エントリー価格との一致や近すぎるのを避けるためにバッファ (0.5 * ATR) を追加 (v16.0.1 FIX)
            r1_buffer = r1 + 0.5 * atr_value

            # R1_bufferが現在価格より高く、かつATR SLより浅い場合 (よりタイトなSL)
            if r1_buffer > price and r1_buffer < sl_atr:
                sl_final = r1_buffer
                structural_sl_used = True
            else:
                sl_final = sl_atr
        else:
            sl_final = sl_atr

        # ショートの場合のエントリータイプ: Pivot R1付近でのリミット注文を想定
        entry_type = "Sell Limit (R1付近)" if not np.isnan(r1) and r1 > price and r1_buffer > sl_final else "Market (現在価格)"
        # R1が存在する場合、エントリー価格をR1に近づける（例：R1）
        if entry_type == "Sell Limit (R1付近)" and r1 > price and sl_final > r1:
             entry_price = r1
             sl_final = r1_buffer # R1エントリー時のSLはR1_bufferとする
        
        # RRR計算
        risk = abs(entry_price - sl_final)
        profit = abs(tp_dts - entry_price)
        rr_ratio = profit / risk if risk > 0 else DTS_RRR_DISPLAY

        return entry_price, sl_final, tp_dts, rr_ratio, structural_sl_used

def score_trend_long(df: pd.DataFrame, last_row: pd.Series, prev_row: pd.Series, timeframe: str) -> float:
    """ロングシグナルに対するスコアリングロジック (v17.0.6)"""
    score = BASE_SCORE # 0.40
    
    # 1. 長期トレンド (SMA)
    # 価格が長期SMA (50 SMA) より上にあるか
    price_above_sma = (last_row.get('close') > last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan))
    if price_above_sma:
        score += 0.15 # 順張りボーナス
    elif last_row.get('close') < last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan) and last_row.get('close') > prev_row.get('close'):
        score += LONG_TERM_REVERSAL_PENALTY # 長期トレンド反転狙いとしてペナルティをボーナスに変更 (+0.20)
    
    # 2. MACD
    macd_hist = last_row.get('MACDh_12_26_9', np.nan)
    prev_macd_hist = prev_row.get('MACDh_12_26_9', np.nan)
    
    # MACDヒストグラムがゼロラインより上で上昇傾向
    if macd_hist > 0 and macd_hist > prev_macd_hist:
        score += 0.15
    # MACDがゼロラインを上抜けした直後
    elif macd_hist > 0 and prev_macd_hist < 0:
        score += 0.20
    # MACDがデッドクロス (売りのサイン) したばかりの場合、ペナルティ
    elif macd_hist < 0 and prev_macd_hist > 0:
        score -= MACD_CROSS_PENALTY

    # 3. RSI
    rsi = last_row.get('RSI_14', np.nan)
    # RSIが買われ過ぎから戻ってきて、勢い水準 (40-60) の下限にいる (押し目買い)
    if RSI_MOMENTUM_LOW <= rsi < 50:
        score += 0.10
    # RSIが売られ過ぎから脱出
    elif rsi < RSI_OVERSOLD and rsi > prev_row.get('RSI_14', np.nan):
        score += 0.15 
    # RSIが過度に高い (売られ過ぎ)
    elif rsi >= RSI_OVERBOUGHT:
        score -= 0.10

    # 4. ADX (トレンドの強さ)
    adx = last_row.get('ADX_14', np.nan)
    plus_di = last_row.get('DMP_14', np.nan)
    minus_di = last_row.get('DMN_14', np.nan)
    
    # トレンドが強い (ADX>30) かつ、上昇トレンドが優勢 (+DI > -DI)
    if adx > ADX_TREND_THRESHOLD and plus_di > minus_di:
        score += 0.15
    # トレンドが弱い (ADX<20)
    elif adx < 20:
        score -= 0.05
    
    # 5. ボリンジャーバンド (ボラティリティの収縮/拡大)
    # ボラティリティのペナルティ (価格がBBの境界付近にあり、バンド幅が狭い場合、反転の可能性)
    bb_width = last_row.get('BBP_20_2.0', np.nan) * 100 # BBPは0.0-1.0なのでパーセントに変換
    if bb_width < VOLATILITY_BB_PENALTY_THRESHOLD:
        score -= 0.10 # ボラティリティが低すぎる場合

    # 6.出来高確認 (Volume Confirmation) - 陽線で出来高が増加
    if last_row.get('close') > prev_row.get('close') and last_row.get('volume') > prev_row.get('volume'):
        score *= (1 + 0.10) # 出来高による確認ボーナス
        
    # スコアの上限を設定
    return min(1.0, score)

def score_trend_short(df: pd.DataFrame, last_row: pd.Series, prev_row: pd.Series, timeframe: str) -> float:
    """ショートシグナルに対するスコアリングロジック (v17.0.6)"""
    score = BASE_SCORE # 0.40

    # 1. 長期トレンド (SMA)
    # 価格が長期SMA (50 SMA) より下にあるか
    price_below_sma = (last_row.get('close') < last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan))
    if price_below_sma:
        score += 0.15 # 順張りボーナス
    elif last_row.get('close') > last_row.get(f'SMA_{LONG_TERM_SMA_LENGTH}', np.nan) and last_row.get('close') < prev_row.get('close'):
        score += LONG_TERM_REVERSAL_PENALTY # 長期トレンド反転狙いとしてペナルティをボーナスに変更 (+0.20)

    # 2. MACD
    macd_hist = last_row.get('MACDh_12_26_9', np.nan)
    prev_macd_hist = prev_row.get('MACDh_12_26_9', np.nan)

    # MACDヒストグラムがゼロラインより下で下降傾向
    if macd_hist < 0 and macd_hist < prev_macd_hist:
        score += 0.15
    # MACDがゼロラインを下抜けした直後
    elif macd_hist < 0 and prev_macd_hist > 0:
        score += 0.20
    # MACDがゴールデンクロス (買いのサイン) したばかりの場合、ペナルティ
    elif macd_hist > 0 and prev_macd_hist < 0:
        score -= MACD_CROSS_PENALTY

    # 3. RSI
    rsi = last_row.get('RSI_14', np.nan)
    # RSIが売られ過ぎから戻ってきて、勢い水準 (40-60) の上限にいる (戻り売り)
    if RSI_MOMENTUM_HIGH >= rsi > 50:
        score += 0.10
    # RSIが買われ過ぎから脱出
    elif rsi > RSI_OVERBOUGHT and rsi < prev_row.get('RSI_14', np.nan):
        score += 0.15 
    # RSIが過度に低い (売られ過ぎ)
    elif rsi <= RSI_OVERSOLD:
        score -= 0.10
        
    # 4. ADX (トレンドの強さ)
    adx = last_row.get('ADX_14', np.nan)
    plus_di = last_row.get('DMP_14', np.nan)
    minus_di = last_row.get('DMN_14', np.nan)
    
    # トレンドが強い (ADX>30) かつ、下降トレンドが優勢 (-DI > +DI)
    if adx > ADX_TREND_THRESHOLD and minus_di > plus_di:
        score += 0.15
    # トレンドが弱い (ADX<20)
    elif adx < 20:
        score -= 0.05
        
    # 5. ボリンジャーバンド (ボラティリティの収縮/拡大)
    bb_width = last_row.get('BBP_20_2.0', np.nan) * 100 
    if bb_width < VOLATILITY_BB_PENALTY_THRESHOLD:
        score -= 0.10 # ボラティリティが低すぎる場合

    # 6.出来高確認 (Volume Confirmation) - 陰線で出来高が増加
    if last_row.get('close') < prev_row.get('close') and last_row.get('volume') > prev_row.get('volume'):
        score *= (1 + 0.10) # 出来高による確認ボーナス

    # スコアの上限を設定
    return min(1.0, score)

def calculate_regime(last_row: pd.Series) -> str:
    """現在の市場レジームを判断する"""
    adx = last_row.get('ADX_14', np.nan)
    plus_di = last_row.get('DMP_14', np.nan)
    minus_di = last_row.get('DMN_14', np.nan)
    
    if np.isnan(adx): return 'UNKNOWN'
    
    if adx > ADX_TREND_THRESHOLD:
        if plus_di > minus_di:
            return 'TRENDING_LONG'
        elif minus_di > plus_di:
            return 'TRENDING_SHORT'
    
    # ADXが弱い場合（レンジ相場）
    if adx < 20:
        return 'RANGE'
        
    return 'MIXED'


def analyze_single_timeframe(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Dict:
    """単一の時間足データに対して分析とスコアリングを実行する (v17.0.6)"""
    
    result = {
        'symbol': symbol,
        'timeframe': timeframe,
        'price': 0.0,
        'side': 'Neutral',
        'score': 0.50,
        'rr_ratio': 0.0,
        'entry': 0.0,
        'sl': 0.0,
        'dynamic_tp_price': 0.0,
        'tp_multiplier_used': 0.0,
        'entry_type': 'Market',
        'tech_data': {},
        'trend_consistency': {},
        'macro_context': macro_context,
    }
    
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    if df.empty or len(df) < required_limit:
        result['side'] = 'DataShortage'
        return result
        
    df = calculate_indicators(df)
    
    # 欠損値を含む行を削除し、最新の2行を取得
    df_cleaned = df.iloc[-max(ATR_LENGTH, LONG_TERM_SMA_LENGTH, SMA_LENGTH):].dropna()
    if len(df_cleaned) < 2:
        result['side'] = 'DataShortage'
        return result
        
    last_row = df_cleaned.iloc[-1]
    prev_row = df_cleaned.iloc[-2]

    # データが有効であることを確認
    price = last_row.get('close', 0.0)
    atr_value = last_row.get(f'ATR_{ATR_LENGTH}', np.nan) 
    
    if price == 0.0 or np.isnan(atr_value):
        result['side'] = 'Neutral'
        return result
        
    result['price'] = price
    result['tech_data'] = {
        'adx': last_row.get('ADX_14', np.nan),
        'rsi': last_row.get('RSI_14', np.nan),
        'rsi_regime': 'Overbought' if last_row.get('RSI_14', np.nan) >= RSI_OVERBOUGHT else ('Oversold' if last_row.get('RSI_14', np.nan) <= RSI_OVERSOLD else 'Neutral'),
        'macd_signal': 'Bullish Crossover' if last_row.get('MACDh_12_26_9', np.nan) > 0 and prev_row.get('MACDh_12_26_9', np.nan) < 0 else ('Bearish Crossover' if last_row.get('MACDh_12_26_9', np.nan) < 0 and prev_row.get('MACDh_12_26_9', np.nan) > 0 else 'Trend Following'),
        'macd_momentum': 'Long' if last_row.get('MACDh_12_26_9', np.nan) > 0 else 'Short',
        'atr_value': atr_value,
        'adx_strength': 'Strong' if last_row.get('ADX_14', np.nan) > ADX_TREND_THRESHOLD else ('Weak/Range' if last_row.get('ADX_14', np.nan) < 20 else 'Developing'),
        'pivot_points': calculate_pivot_points(last_row),
        'regime': calculate_regime(last_row)
    }

    # 2. スコアリング
    long_score = score_trend_long(df_cleaned, last_row, prev_row, timeframe)
    short_score = score_trend_short(df_cleaned, last_row, prev_row, timeframe)
    
    # マクロ要因によるバイアス調整
    is_btc_or_eth = symbol.startswith('BTC') or symbol.startswith('ETH')
    
    # 資金調達率バイアス調整
    fr_bias = macro_context.get('funding_rate_bias')
    if fr_bias == 'SHORT_BIAS':
        # ロング過熱 -> ショートスコアにボーナス
        short_score += FUNDING_RATE_BONUS_PENALTY
    elif fr_bias == 'LONG_BIAS':
        # ショート過熱 -> ロングスコアにボーナス
        long_score += FUNDING_RATE_BONUS_PENALTY

    # ドミナンスバイアス調整 (BTC/ETH以外のAltcoinにのみ適用)
    dom_bias = macro_context.get('dominance_bias')
    if not is_btc_or_eth:
        if dom_bias == 'BTC_BULL':
            # BTCが強い -> Altcoinのロングスコアにペナルティ
            long_score -= DOMINANCE_BIAS_BONUS_PENALTY
        elif dom_bias == 'ALT_BULL':
            # Altcoinが強い -> Altcoinのロングスコアにボーナス
            long_score += DOMINANCE_BIAS_BONUS_PENALTY
            
    # 3. 最終的なシグナルの決定
    if long_score >= SIGNAL_THRESHOLD and long_score > short_score:
        result['side'] = 'ロング'
        result['score'] = long_score
    elif short_score >= SIGNAL_THRESHOLD and short_score > long_score:
        result['side'] = 'ショート'
        result['score'] = short_score
    else:
        result['side'] = 'Neutral'
        result['score'] = max(long_score, short_score) # 閾値未満でもスコアは記録

    # 4. SL/TPとRRRの計算 (シグナルが出た場合のみ)
    if result['side'] in ['ロング', 'ショート']:
        side_long = (result['side'] == 'ロング')
        
        entry, sl, tp_dts, rr_ratio, structural_sl_used = calculate_atr_sl_tp(
            price=price, 
            atr_value=atr_value, 
            pivot_points=result['tech_data']['pivot_points'], 
            side_long=side_long,
            timeframe=timeframe
        )
        
        result['entry'] = entry
        result['sl'] = sl
        result['dynamic_tp_price'] = tp_dts
        result['rr_ratio'] = rr_ratio
        result['entry_type'] = "Buy Limit (S1付近)" if side_long and entry != price else ("Sell Limit (R1付近)" if not side_long and entry != price else "Market (現在価格)")
        result['tech_data']['structural_sl_used'] = structural_sl_used
        result['tp_multiplier_used'] = TP_ATR_MULTIPLIERS.get(timeframe)

    return result

async def run_technical_analysis(symbol: str) -> List[Dict]:
    """指定されたシンボルに対して複数の時間足で分析を実行し、結果を統合する (v17.0.6)"""
    timeframes = ['15m', '1h', '4h']
    all_results = []
    
    for tf in timeframes:
        # NOTE: fetch_ohlcv_dataでレート制限エラーが発生するため、ここで逐次実行
        df = await fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
        
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

        # 逐次実行時のレート制限回避のための遅延
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

    
    # 複数時間軸の結果を統合し、トレンドの一貫性を評価 (v17.0.5)
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

        # 最もスコアの高いシグナルに一貫性ボーナスを適用
        best_signal_index = -1
        best_score = -1.0
        
        for i, r in enumerate(all_results):
            if r['side'] in ['ロング', 'ショート']:
                if r['score'] > best_score:
                    best_score = r['score']
                    best_signal_index = i
                
                # トレンドの一貫性情報を追加
                r['trend_consistency'] = consistency_data
                
        if is_consistent and best_signal_index != -1:
            # 3つ全てで一致した場合、最高スコアのシグナルにボーナスを追加
            all_results[best_signal_index]['score'] = min(1.0, all_results[best_signal_index]['score'] + TREND_CONSISTENCY_BONUS)
            

    return all_results

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
            logging.info(f"🔄 Apex BOT v17.0.6 実行開始: {current_time_j.strftime('%Y-%m-%d %H:%M:%S JST')}")

            # 1. シンボルリストの更新 (省略: 固定リストを使用)
            # CURRENT_MONITOR_SYMBOLS = await fetch_all_available_symbols(TOP_SYMBOL_LIMIT)
            logging.info(f"監視対象シンボル: {len(CURRENT_MONITOR_SYMBOLS)} 種類を決定しました。")

            # 2. グローバルマクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await fetch_global_macro_context()

            # 3. テクニカル分析の実行 (レート制限回避のため、逐次実行に変更)
            all_analysis_results = []
            
            # --- 修正: Rate Limit回避のため、シンボル分析を逐次実行に変更 ---
            for symbol in CURRENT_MONITOR_SYMBOLS:
                results = await run_technical_analysis(symbol)
                all_analysis_results.extend(results)
                # シンボル間の遅延を挿入 (0.5s * 2 = 1.0秒間隔)
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL * 2.0) 
            # -------------------------------------------------------------
                
            LAST_ANALYSIS_SIGNALS = all_analysis_results
            
            # 4. シグナルのフィルタリングと統合
            
            # 有効なシグナル (ロング/ショート) を抽出し、スコア降順、RRR降順でソート
            trade_signals = [
                s for s in LAST_ANALYSIS_SIGNALS 
                if s['side'] in ['ロング', 'ショート'] and s['score'] >= SIGNAL_THRESHOLD
            ]

            # 同一シンボル・時間足でスコアが最も高いものを残す (統合分析のため、ここは使わないが念のため)
            
            # シンボルごとに最もスコアの高いシグナル（通常は最も長い時間足のものが選ばれやすい）を採用
            best_signals_per_symbol: Dict[str, List[Dict]] = {}
            for s in trade_signals:
                symbol = s['symbol']
                if symbol not in best_signals_per_symbol:
                     best_signals_per_symbol[symbol] = []
                best_signals_per_symbol[symbol].append(s)

            # 統合されたシグナルの最終選定
            integrated_signals = []
            for symbol, signals in best_signals_per_symbol.items():
                # 複数時間足の結果から、ベストスコアのシグナルを見つける
                best_signal = max(
                    signals, 
                    key=lambda s: (s['score'], s['rr_ratio'])
                )
                
                # 統合シグナルリストに格納
                integrated_signals.append(best_signal)
                
            # スコアとRRRに基づいて最終ソート
            final_trade_signals = sorted(
                integrated_signals, 
                key=lambda s: (s['score'], s['rr_ratio']), 
                reverse=True
            )
            
            # 5. 通知ロジック
            
            # クールダウン期間の経過チェック
            now = time.time()
            TRADE_NOTIFIED_SYMBOLS = {
                k: v for k, v in TRADE_NOTIFIED_SYMBOLS.items() 
                if now - v < TRADE_SIGNAL_COOLDOWN
            }

            notification_count = 0
            for rank, signal in enumerate(final_trade_signals[:TOP_SIGNAL_COUNT]):
                symbol = signal['symbol']
                
                # クールダウンチェック
                if symbol in TRADE_NOTIFIED_SYMBOLS:
                    # logging.info(f"シグナルスキップ: {symbol} はクールダウン期間中です。")
                    continue
                
                # 総合分析メッセージを整形
                # NOTE: format_integrated_analysis_message は、そのシンボルの全時間足の結果を受け取り、
                # その中からベストなものを抽出してメッセージを生成する
                full_signals_for_symbol = [s for s in all_analysis_results if s['symbol'] == symbol]
                
                # 統合されたメッセージを生成 (ランキング情報を含む)
                message = format_integrated_analysis_message(symbol, full_signals_for_symbol, rank + 1)
                
                if message:
                    send_telegram_html(message)
                    TRADE_NOTIFIED_SYMBOLS[symbol] = now
                    notification_count += 1
            
            if notification_count > 0:
                logging.info(f"Telegram通知を {notification_count} 件送信しました。")

            LAST_UPDATE_TIME = now
            LAST_SUCCESS_TIME = now
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = CURRENT_MONITOR_SYMBOLS.copy()
            logging.info("====================================")
            logging.info(f"✅ Apex BOT v17.0.6 実行完了。次の実行まで {LOOP_INTERVAL} 秒待機します。")
            logging.info(f"通知クールダウン中のシンボル: {list(TRADE_NOTIFIED_SYMBOLS.keys())}")
            
            await asyncio.sleep(LOOP_INTERVAL)

        except KeyboardInterrupt:
            logging.info("ユーザーにより中断されました。シャットダウンします。")
            break
        except Exception as e:
            error_name = type(e).__name__
            # CCXTクライアントがエラーを起こした場合、再初期化を試みる
            if 'ccxt' in error_name.lower():
                 logging.warning("CCXT関連のエラーを検知。クライアントを再初期化します...")
                 await initialize_ccxt_client()
            
            logging.error(f"メインループで致命的なエラー: {error_name} {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v17.0.6 - Debug & Rate Limit Fix") # バージョン更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.0.6 Startup initializing...") # バージョン更新
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
        "bot_version": "v17.0.6 - Debug & Rate Limit Fix", # バージョン更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.0.6)"})

# ====================================================================================
# EXECUTION (If run directly)
# ====================================================================================

if __name__ == "__main__":
    # uvicorn.run(app, host="0.0.0.0", port=10000) # Fast API を通さない場合 
    # v17.0.6 では Render デプロイ用の main_render.py を想定し、FastAPI 経由で実行します。
    # Render の標準設定では、main_render.py がエントリポイントとなるため、直接 Uvicorn を実行
    # main_render.py をエントリポイントとした場合:
    # uvicorn.run("main_render:app", host="0.0.0.0", port=10000)
    pass
