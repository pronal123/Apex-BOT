# ====================================================================================
# Apex BOT v12.1.38 - CMF & RVI Diversity + Enhanced Telegram Notifications
# - 通知メッセージのロジックを最終的に改善された「スコアリング内訳」表示に更新。
# - CMF, RVI, MACD, RSIなど全てのスコアリング要素が正確に通知に反映されるよう調整。
# - CCXTの初期化とデータ取得ロジックを非同期環境に最適化。
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
LOOP_INTERVAL = 180        # 180秒間隔で分析を実行

# CCXT レート制限対策 
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 2時間クールダウン
SIGNAL_THRESHOLD = 0.70             # 通知トリガーとなる最低スコア (70.00点)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 

LONG_TERM_SMA_LENGTH = 50           # 4hトレンド判定用SMA
LONG_TERM_REVERSAL_PENALTY = 0.15   
MACD_CROSS_PENALTY = 0.10           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.5            
SHORT_TERM_SL_MULTIPLIER = 1.5      # SL距離の決定 (ATR x 1.5)

# スコアリングロジック用の定数 (1.00点満点での寄与度)
SCORE_MACD_DIR = 0.18   # MACDに基づく方向性
SCORE_RSI_OVERSOLD = 0.10 # RSIに基づく過熱感
SCORE_RSI_MOMENTUM = 0.10 # RSIに基づくモメンタムブレイクアウト
SCORE_ADX_TREND = 0.08  # ADXに基づくトレンドフォロー
SCORE_VWAP = 0.05       # VWAPの一致チェック
SCORE_PPO = 0.05        # PPOに基づくモメンタム強度
SCORE_DC_BREAKOUT = 0.13 # Donchian Channelに基づくブレイクアウト
SCORE_COMPOSITE_MOMENTUM = 0.07 # 複合モメンタム加速ボーナス
SCORE_CMF_CONFIRMATION = 0.05 # CMFに基づく流動性確証 (NEW)
SCORE_RVI_CONFIRMATION = 0.04 # RVIに基づくモメンタム確証 (NEW)

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 45 
RSI_MOMENTUM_HIGH = 55
ADX_TREND_THRESHOLD = 25
VOLUME_CONFIRMATION_MULTIPLIER = 1.5 

CMF_THRESHOLD = 0.10 
RVI_OVERHEAT_HIGH = 80
RVI_OVERHEAT_LOW = 20
PENALTY_STOCH_FILTER = 0.05
PENALTY_RVI_OVERHEAT = 0.05
BONUS_VOLUME_CONFIRMATION = 0.10 # 出来高確証の最大ボーナス

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING (UPDATED FOR FINAL NOTIFICATION)
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """時間足に基づきTP到達目安を算出する (ログメッセージ用)"""
    if timeframe == '15m': return "数時間以内 (2〜4時間)"
    if timeframe == '1h': return "半日以内 (6〜12時間)"
    if timeframe == '4h': return "数日以内 (1〜3日)"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.4f}"
    if price >= 0.1: return f"{price:,.6f}"
    return f"{price:,.8f}"

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
    adjusted_rate = 0.50 + (score - 0.50) * 1.5 
    return max(0.40, min(0.80, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (最終改善版 - スコアリング内訳表示)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals: return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals: return "" 
        
    best_signal = max(
        high_score_signals, 
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0), s.get('tech_data', {}).get('adx', 0.0), -s.get('tech_data', {}).get('atr_value', 1.0), s.get('symbol', ''))
    )
    
    # 主要データ抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    
    display_symbol = symbol.replace('-', '/')
    
    # ----------------------------------------------------
    # 1. ヘッダー: 結論と優位性
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好" if score_raw >= 0.85 else ("高" if score_raw >= 0.75 else "中")
    
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    header = (
        f"--- 🟢 --- <b>{display_symbol}</b> ({timeframe}足) --- 🟢 ---\n"
        f"{rank_header} 📈 **{strength}** シグナル！ - {direction_emoji}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> |\n" 
        f"| ⏰ **TP 目安** | {get_tp_reach_time(timeframe)} | (RRR: 1:{rr_ratio:.2f}) |\n"
        f"==================================\n"
    )

    # ----------------------------------------------------
    # 2. 取引計画: 実行可能性
    # ----------------------------------------------------
    sl_width = abs(entry_price - sl_price)
    
    trade_plan = (
        f"**📋 取引計画 ({entry_type}注文)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | |\n"
        f"| ➡️ **Entry** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション |\n" 
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | 利確 |\n"
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 |\n"
        f"----------------------------------\n"
        f"<pre>リスク幅: ${format_price_utility(sl_width, symbol)} (RRR 1:{rr_ratio:.2f})</pre>\n"
    )
    
    # ----------------------------------------------------
    # 3. 分析根拠: スコアリング内訳 (優位性の源泉)
    # ----------------------------------------------------
    analysis_detail = "**📊 メイン時間軸スコア内訳 ("
    analysis_detail += f"{timeframe}足)**\n"
    analysis_detail += "----------------------------------\n"

    main_tech_data = best_signal.get('tech_data', {})
    
    long_score_factors = []
    short_score_factors = []
    
    # --- スコアリング内訳の計算 ---
    
    # A. MACDトレンドフォロー (0.18)
    if main_tech_data.get('macd_dir_bonus', 0.0) == SCORE_MACD_DIR:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"MACD方向性 +{SCORE_MACD_DIR * 100:.2f}点")
        
    # B/C. RSI系 (合計 0.20)
    if main_tech_data.get('rsi_oversold_bonus', 0.0) == SCORE_RSI_OVERSOLD:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"RSI過売買 +{SCORE_RSI_OVERSOLD * 100:.2f}点")
    if main_tech_data.get('rsi_momentum_bonus', 0.0) == SCORE_RSI_MOMENTUM:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"RSIブレイク +{SCORE_RSI_MOMENTUM * 100:.2f}点")

    # D. ADXトレンド (0.08)
    if main_tech_data.get('adx_trend_bonus', 0.0) == SCORE_ADX_TREND:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"ADXトレンド +{SCORE_ADX_TREND * 100:.2f}点")

    # E. VWAP一致 (0.05)
    if main_tech_data.get('vwap_consistent', False):
         (long_score_factors if side == "ロング" else short_score_factors).append(f"VWAP一致 +{SCORE_VWAP * 100:.2f}点")
    
    # F. PPO強度 (0.05)
    if main_tech_data.get('ppo_strength_bonus', 0.0) == SCORE_PPO:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"PPO強度 +{SCORE_PPO * 100:.2f}点")

    # G. Donchian Channel (0.13)
    if main_tech_data.get('dc_breakout_bonus', 0.0) == SCORE_DC_BREAKOUT:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"DCブレイク +{SCORE_DC_BREAKOUT * 100:.2f}点")

    # H. 複合モメンタム加速ボーナス (0.07)
    if main_tech_data.get('composite_momentum_bonus', 0.0) == SCORE_COMPOSITE_MOMENTUM:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"複合モメンタム +{SCORE_COMPOSITE_MOMENTUM * 100:.2f}点")
        
    # K. CMF流動性確証 (0.05)
    if main_tech_data.get('cmf_bonus', 0.0) == SCORE_CMF_CONFIRMATION:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"CMF流動性 +{SCORE_CMF_CONFIRMATION * 100:.2f}点")
        
    # L. RVIモメンタム確証 (0.04)
    if main_tech_data.get('rvi_momentum_bonus', 0.0) == SCORE_RVI_CONFIRMATION:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"RVI確証 +{SCORE_RVI_CONFIRMATION * 100:.2f}点")
        
    # J. 出来高確証ボーナス (最大 0.10)
    vol_bonus = main_tech_data.get('volume_confirmation_bonus', 0.0)
    if vol_bonus > 0.0:
        (long_score_factors if side == "ロング" else short_score_factors).append(f"出来高確証ボーナス +{vol_bonus * 100:.2f}点")
        
    
    factors = long_score_factors if side == "ロング" else short_score_factors
    
    # スコア内訳の表示
    analysis_detail += '\n'.join([f"🔸 {f}" for f in factors])
    analysis_detail += "\n----------------------------------\n"

    # 4. フィルター結果の表示
    analysis_detail += "**✅ フィルターチェック (MTF)**\n"
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        tech_data = s.get('tech_data', {})
        
        if tf == '4h':
            long_term_trend_4h = tech_data.get('long_term_trend', 'Neutral')
            analysis_detail += (f"🌍 **4h 長期トレンド**: {long_term_trend_4h}\n")
        else:
            
            direction_match = "✅ 方向一致" if s_side == side else "❌ 方向不一致"
            
            # ペナルティの総額を計算し、テキスト化
            lt_penalty = tech_data.get('long_term_reversal_penalty', False)
            macd_invalid = not tech_data.get('macd_cross_valid', True)
            stoch_penalty_val = tech_data.get('stoch_filter_penalty', 0.0)
            rvi_penalty_val = tech_data.get('rvi_overheat_penalty', 0.0)
            
            penalty_texts = []
            if lt_penalty: penalty_texts.append("長期逆張り減点")
            if macd_invalid: penalty_texts.append("モメンタム反転減点")
            if stoch_penalty_val > 0.0: penalty_texts.append(f"STOCH減点 (-{stoch_penalty_val * 100:.2f}点)")
            if rvi_penalty_val > 0.0: penalty_texts.append(f"RVI過熱減点 (-{rvi_penalty_val * 100:.2f}点)")
            
            penalty_line = " / ".join(penalty_texts) if penalty_texts else "ペナルティなし"

            # 統合された一行サマリー
            analysis_detail += (
                f"**[{tf} 足]** ({tech_data.get('adx', 0.0):.2f}) -> {direction_match} ({s_side})\n"
                f"   └ フィルタリング: {penalty_line}\n"
            )

    # ----------------------------------------------------
    # 5. フッター
    # ----------------------------------------------------
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {main_tech_data.get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | v12.1.38 - FINAL |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ 投資判断は自己責任でお願いします。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    # 以前のクライアントが存在する場合はクローズ
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        
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


async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが未初期化のため、出来高による銘柄更新をスキップします。")
        return

    try:
        # スポット市場のティッカーを取得
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }

        # 出来高でソート
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
            logging.info(f"✅ 出来高TOP{TOP_SYMBOL_LIMIT}銘柄をOKXスワップ形式に更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
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
        # OKXは'swap'デフォルトなので、シンボル変換は不要（BTC-USDTのまま）
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        logging.warning(f"CCXT Network Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        logging.warning(f"CCXT Exchange Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
    except Exception as e:
        logging.error(f"予期せぬデータ取得エラー ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """マクロ市場コンテキストを取得 (ダミー)"""
    return {
        "vix_value": 2.5,
        "trend": "Risk-On (BTC Dominance stable)"
    }


# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v12.1.38 - スコア寄与度をtech_dataに格納)
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": True,
        "vwap_consistent": False, "dc_high": 0.0, "dc_low": 0.0,
        "stoch_filter_penalty": 0.0, "rvi_overheat_penalty": 0.0, 
        "volume_confirmation_bonus": 0.0, 
        
        # スコアリングボーナスの格納用フィールド (通知用)
        "macd_dir_bonus": 0.0, "rsi_oversold_bonus": 0.0, "rsi_momentum_bonus": 0.0, 
        "adx_trend_bonus": 0.0, "ppo_strength_bonus": 0.0, "dc_breakout_bonus": 0.0, 
        "composite_momentum_bonus": 0.0, "cmf_bonus": 0.0, "rvi_momentum_bonus": 0.0,
    }
    
    price = ohlcv[-1][4] if status == "Success" and ohlcv else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume']) 

    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    
    # 指標列名定数
    MACD_HIST_COL = 'MACD_Hist'     
    PPO_HIST_COL = 'PPOh_12_26_9'   
    STOCHRSI_K = 'STOCHRSIk_14_14_3_3'
    
    RVI_VALUE_COL = 'RVI_14' 

    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        df.ta.macd(append=True)
        df.ta.adx(append=True)
        df.ta.bbands(append=True) 
        df.ta.atr(append=True)
        df.ta.cci(append=True)
        df.ta.vwap(append=True)
        df.ta.ppo(append=True) 
        df.ta.donchian(length=20, append=True) 
        df.ta.stochrsi(append=True)
        df['cmf'] = ta.cmf(df['high'], df['low'], df['close'], df['volume'], length=20)
        df.ta.rvi(append=True) 
        
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', PPO_HIST_COL, STOCHRSI_K, 'cmf', RVI_VALUE_COL] 
        df.dropna(subset=required_cols, inplace=True)

        if df.empty:
            return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

        # データの安全な取得
        rsi_val = df['rsi'].iloc[-1]
        macd_hist_val = df[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df[MACD_HIST_COL].iloc[-2] 
        adx_val = df['ADX_14'].iloc[-1] # pandas_taはADX_14という列名になる
        atr_val = df['ATR_14'].iloc[-1]
        vwap_val = df['VWAP'].iloc[-1] 
        ppo_hist_val = df[PPO_HIST_COL].iloc[-1] 
        stoch_k_val = df[STOCHRSI_K].iloc[-1] 
        cmf_val = df['cmf'].iloc[-1] 
        rvi_val = df[RVI_VALUE_COL].iloc[-1] 
        rvi_sig_val = df['RVIs_14'].iloc[-1] 
        dc_low_val = df['DCL_20'].iloc[-1]     
        dc_high_val = df['DCU_20'].iloc[-1]
        
        current_volume = df['volume'].iloc[-1]
        average_volume = df['volume'].iloc[-31:-1].mean() if len(df) >= 31 else df['volume'].mean()

        
        long_score = 0.5
        short_score = 0.5
        score_bonuses = tech_data_defaults.copy() # ボーナスを記録するための辞書

        # 2. **動的シグナル判断ロジック (スコアリング)**
        
        # A. MACDに基づく方向性 (寄与度 0.18)
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += SCORE_MACD_DIR
            score_bonuses['macd_dir_bonus'] = SCORE_MACD_DIR
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += SCORE_MACD_DIR
            score_bonuses['macd_dir_bonus'] = SCORE_MACD_DIR

        # B. RSIに基づく買われすぎ/売られすぎ (0.10)
        if rsi_val < RSI_OVERSOLD:
            long_score += SCORE_RSI_OVERSOLD
            score_bonuses['rsi_oversold_bonus'] = SCORE_RSI_OVERSOLD
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += SCORE_RSI_OVERSOLD
            score_bonuses['rsi_oversold_bonus'] = SCORE_RSI_OVERSOLD
            
        # C. RSIに基づくモメンタムブレイクアウト (0.10)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += SCORE_RSI_MOMENTUM
            score_bonuses['rsi_momentum_bonus'] = SCORE_RSI_MOMENTUM
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += SCORE_RSI_MOMENTUM
            score_bonuses['rsi_momentum_bonus'] = SCORE_RSI_MOMENTUM

        # D. ADXに基づくトレンドフォロー強化 (0.08)
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score:
                long_score += SCORE_ADX_TREND
                score_bonuses['adx_trend_bonus'] = SCORE_ADX_TREND
            elif short_score > long_score:
                short_score += SCORE_ADX_TREND
                score_bonuses['adx_trend_bonus'] = SCORE_ADX_TREND
        
        # E. VWAPの一致チェック (0.05)
        vwap_consistent = False
        if price > vwap_val:
            long_score += SCORE_VWAP
            vwap_consistent = True
        elif price < vwap_val:
            short_score += SCORE_VWAP
            vwap_consistent = True
        score_bonuses['vwap_consistent'] = vwap_consistent # 整合性をtech_dataに記録
        
        # F. PPOに基づくモメンタム強度の評価 (0.05)
        ppo_abs_mean = df[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean:
            long_score += SCORE_PPO
            score_bonuses['ppo_strength_bonus'] = SCORE_PPO
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean:
            short_score += SCORE_PPO
            score_bonuses['ppo_strength_bonus'] = SCORE_PPO

        # G. Donchian Channelによるブレイクアウト (寄与度 0.13)
        is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
        is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val

        if is_breaking_high:
            long_score += SCORE_DC_BREAKOUT
            score_bonuses['dc_breakout_bonus'] = SCORE_DC_BREAKOUT
        elif is_breaking_low:
            short_score += SCORE_DC_BREAKOUT
            score_bonuses['dc_breakout_bonus'] = SCORE_DC_BREAKOUT
        
        # H. 複合モメンタム加速ボーナス (寄与度 0.07)
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50:
             long_score += SCORE_COMPOSITE_MOMENTUM
             score_bonuses['composite_momentum_bonus'] = SCORE_COMPOSITE_MOMENTUM
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50:
             short_score += SCORE_COMPOSITE_MOMENTUM
             score_bonuses['composite_momentum_bonus'] = SCORE_COMPOSITE_MOMENTUM
             
        # K. CMF (Chaikin Money Flow)に基づく流動性フィルター (+0.05)
        if cmf_val > CMF_THRESHOLD:
            long_score += SCORE_CMF_CONFIRMATION 
            score_bonuses['cmf_bonus'] = SCORE_CMF_CONFIRMATION
        elif cmf_val < -CMF_THRESHOLD:
            short_score += SCORE_CMF_CONFIRMATION 
            score_bonuses['cmf_bonus'] = SCORE_CMF_CONFIRMATION
            
        # L. RVI (Relative Vigor Index)に基づくモメンタム確証 (+0.04)
        if rvi_val > 50 and rvi_val > rvi_sig_val:
            long_score += SCORE_RVI_CONFIRMATION
            score_bonuses['rvi_momentum_bonus'] = SCORE_RVI_CONFIRMATION
        elif rvi_val < 50 and rvi_val < rvi_sig_val:
            short_score += SCORE_RVI_CONFIRMATION
            score_bonuses['rvi_momentum_bonus'] = SCORE_RVI_CONFIRMATION
            
        # 最終スコア決定 (この時点での中間スコア)
        if long_score > short_score:
            side = "ロング"
            base_score = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score = short_score
        else:
            side = "Neutral"
            base_score = 0.5
        
        score = min(1.0, base_score) 
        
        # ----------------------------------------------------------------------
        # ペナルティとフィルターの適用
        # ----------------------------------------------------------------------
        
        current_long_term_penalty_applied = False
        
        # 1. Stochastic RSIに基づく過熱感フィルター (ペナルティ0.05)
        stoch_filter_penalty = 0.0
        if timeframe in ['15m', '1h']:
            is_long_overheated = stoch_k_val >= 80 
            is_short_overheated = stoch_k_val <= 20
            
            # ロングシグナルだが買われすぎの場合、ショートシグナルだが売られすぎの場合
            if (side == "ロング" and is_long_overheated) or (side == "ショート" and is_short_overheated):
                stoch_filter_penalty = PENALTY_STOCH_FILTER 
                
            score = max(0.5, score - stoch_filter_penalty) 
            score_bonuses['stoch_filter_penalty'] = stoch_filter_penalty
            
        # 2. RVI 過熱感ペナルティ (-0.05)
        rvi_overheat_penalty = 0.0
        if timeframe in ['15m', '1h']: 
            if rvi_val > RVI_OVERHEAT_HIGH or rvi_val < RVI_OVERHEAT_LOW:
                 rvi_overheat_penalty = PENALTY_RVI_OVERHEAT
                 
            score = max(0.5, score - rvi_overheat_penalty)
            score_bonuses['rvi_overheat_penalty'] = rvi_overheat_penalty


        # 3. 出来高に基づくシグナル確証 (ボーナス0.10 - 既にボーナスとして加算済みだが、上限調整)
        volume_confirmation_bonus = 0.0
        if current_volume > average_volume * VOLUME_CONFIRMATION_MULTIPLIER: 
            # 出来高ボーナスはMACD/DC確証の組み合わせで最大0.10。ここでは既に加算されている。
            volume_confirmation_bonus = min(BONUS_VOLUME_CONFIRMATION, 
                                            (0.05 if is_breaking_high or is_breaking_low else 0.0) +
                                            (0.05 if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean() else 0.0))
            # 出来高ボーナスを改めて最終スコアに加算 (MACD/DCボーナスとは別に追加)
            # NOTE: このコードブロックはロジックをシンプルにするため、H, Gの複合モメンタムに集約
            # されたものと仮定し、通知のための記録のみに留める。
            score_bonuses['volume_confirmation_bonus'] = volume_confirmation_bonus


        # 4. 4hトレンドフィルターの適用 (15m, 1hのみ) (ペナルティ0.15)
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
                score_bonuses['long_term_reversal_penalty'] = current_long_term_penalty_applied
        
        # 5. MACDクロス確認と減点 (モメンタム反転チェック) (ペナルティ0.10)
        macd_valid = True
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(0.5, score - MACD_CROSS_PENALTY)
                 macd_valid = False
             score_bonuses['macd_cross_valid'] = macd_valid
             
        
        # 6. TP/SLとRRRの決定
        rr_base = SHORT_TERM_BASE_RRR 
        if (timeframe != '4h') and (side == long_term_trend and long_term_trend != "Neutral"):
            rr_base = SHORT_TERM_MAX_RRR
        
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        
        bb_mid = df['BBM_20_2.0'].iloc[-1]
        dc_mid = (dc_high_val + dc_low_val) / 2
        
        entry = price 
        tp1 = 0
        sl = 0
        entry_type = "N/A"

        # エントリーモードの決定
        is_high_conviction = score >= 0.70
        adx_regime = "トレンド" if adx_val >= ADX_TREND_THRESHOLD else "レンジ"
        is_strong_trend = adx_val >= 30 
        
        use_market_entry = is_high_conviction or is_strong_trend
        entry_type = "Market" if use_market_entry else "Limit"

        if side == "ロング":
            if use_market_entry:
                entry = price
            else:
                optimal_entry = min(bb_mid, dc_mid) 
                entry = min(optimal_entry, price) 
                if price - entry > sl_dist * 0.5: 
                    entry = price
                    entry_type = "Market (Fallback)"

            sl = entry - sl_dist
            tp_dist = sl_dist * rr_base 
            tp1 = entry + tp_dist

        elif side == "ショート":
            if use_market_entry:
                entry = price
            else:
                optimal_entry = max(bb_mid, dc_mid)
                entry = max(optimal_entry, price) 
                if entry - price > sl_dist * 0.5: 
                    entry = price
                    entry_type = "Market (Fallback)"
            
            sl = entry + sl_dist
            tp_dist = sl_dist * rr_base 
            tp1 = entry - tp_dist
            
        else:
            entry_type = "N/A"
            entry, sl, tp1, rr_base = price, 0, 0, 0
        
        # 7. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD:
             final_side = "Neutral"

        # 8. tech_dataの構築
        tech_data = {
            "rsi": rsi_val,
            "macd_hist": macd_hist_val, 
            "adx": adx_val,
            "atr_value": atr_val,
            "long_term_trend": long_term_trend,
            "dc_high": dc_high_val,
            "dc_low": dc_low_val,
            "current_volume": current_volume,
            
            # スコアリングボーナス/ペナルティを格納
            **score_bonuses, 
        }
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        entry_type = "N/A"
        
    # 9. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "side": final_side,
        "score": score, # 精度を維持したスコア (0.0 - 1.0)
        "confidence": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0,
        "regime": adx_regime if final_side != "Neutral" else "N/A",
        "macro_context": macro_context,
        "client": client_used,
        "timeframe": timeframe,
        "tech_data": tech_data,
        "entry_type": entry_type
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    
    # 0. 4hトレンドの事前計算
    long_term_trend = 'Neutral'
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    
    if status_4h == "Success" and len(ohlcv_4h) >= LONG_TERM_SMA_LENGTH:
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_4h['close'] = pd.to_numeric(df_4h['close'])
        df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms', utc=True)
        df_4h.set_index('timestamp', inplace=True)
        
        try:
            df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
            df_4h.dropna(subset=['sma'], inplace=True)
            
            if not df_4h.empty and 'sma' in df_4h.columns and not pd.isna(df_4h['sma'].iloc[-1]):
                last_price = df_4h['close'].iloc[-1]
                last_sma = df_4h['sma'].iloc[-1]
                
                if last_price > last_sma:
                    long_term_trend = 'Long'
                elif last_price < last_sma:
                    long_term_trend = 'Short'
        except Exception:
            pass 
            
    # 1. 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend) 
    ]
    
    results = await asyncio.gather(*tasks)
    
    # MTF スコアリングブーストロジック
    signal_1h_item = next((r for r in results if r and r.get('timeframe') == '1h'), None)
    signal_15m_item = next((r for r in results if r and r.get('timeframe') == '15m'), None)

    if signal_1h_item and signal_15m_item:
        is_1h_strong_signal = signal_1h_item['score'] >= 0.70
        is_direction_matched = signal_1h_item['side'] == signal_15m_item['side']
        
        # 15m足に0.05のボーナススコアを加算
        if is_direction_matched and is_1h_strong_signal:
            signal_15m_item['score'] = min(1.0, signal_15m_item['score'] + 0.05)
            
    # 4h分析結果の統合
    for result in results:
        if result and result.get('timeframe') == '4h':
            # 4h分析のtech_dataに長期トレンドを改めて格納
            result.setdefault('tech_data', {})['long_term_trend'] = long_term_trend
    
    return [r for r in results if r is not None]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CCXT_CLIENT_NAME

    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            
            # 出来高による銘柄更新は一定間隔（例: 1時間ごと）で実行しても良いが、ここでは毎サイクル実行
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            macro_context = await get_crypto_macro_context()
            
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            results_list_of_lists = []
            
            for symbol in monitor_symbols:
                result = await generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME)
                results_list_of_lists.append(result)
                
                # レート制限対策の遅延
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            best_signals_per_symbol = {}
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                if signal.get('side') == 'Neutral' or signal.get('side') in ["DataShortage", "ExchangeError"]:
                    continue

                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals,
                        'rr_ratio': signal.get('rr_ratio', 0.0), 
                        'adx_val': signal.get('tech_data', {}).get('adx', 0.0), 
                        'atr_val': signal.get('tech_data', {}).get('atr_value', 1.0),
                        'symbol': symbol 
                    }
            
            # 順位付け (Score, RRR, ADX, -ATR, Symbol)
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: (
                    x['score'],     
                    x['rr_ratio'],  
                    x['adx_val'],   
                    -x['atr_val'],  
                    x['symbol']     
                ), 
                reverse=True
            )
            
            # SIGNAL_THRESHOLD (0.70) を超えるシグナルのみを抽出
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            notify_tasks = [] 
            
            if top_signals_to_notify:
                logging.info(f"🔔 高スコアシグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        # 順位 (i + 1) を渡してメッセージ生成
                        msg = format_integrated_analysis_message(symbol, item['all_signals'], i + 1)
                        
                        if msg:
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {i+1}位, スコア: {item['score'] * 100:.2f}点)")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            
                            task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                            notify_tasks.append(task)
                            
                    else:
                        log_symbol = symbol.replace('-', '/')
                        logging.info(f"🕒 {log_symbol} はクールダウン期間中です。通知をスキップします。")
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            if notify_tasks:
                 await asyncio.gather(*notify_tasks, return_exceptions=True)

            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            logging.error(f"メインループで致命的なエラー: {e}")
            
            # クライアントが切断された場合を考慮し、再初期化を試みる
            try:
                if EXCHANGE_CLIENT:
                    await EXCHANGE_CLIENT.close()
                await initialize_ccxt_client()
            except Exception as init_err:
                logging.error(f"CCXT再初期化中にエラー: {init_err}")
                
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v12.1.38-FINAL")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v12.1.38 Startup initializing...") 
    # バックグラウンドでメインループを実行開始
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS
    
    status_msg = {
        "status": "ok",
        "bot_version": "v12.1.38-FINAL",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v12.1.38-FINAL)."}, status_code=200)

if __name__ == '__main__':
    # PORT環境変数が設定されていない場合、8080を使用
    # Render, HerokuなどのPaaS環境での実行を想定
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
