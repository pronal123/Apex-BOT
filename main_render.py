# ====================================================================================
# Apex BOT v12.1.30 - スコアの明確化とモメンタム加速ボーナス導入版
# - MACDとDonchian Channelのスコア重み付けを強化し、シグナル間のスコア差を拡大。
# - 複合モメンタム加速ボーナス (MACD+PPO+RSI) を追加。
# - Render環境安定化のため LOOP_INTERVAL=180 に設定。
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
LOOP_INTERVAL = 180        # ★修正: Render安定化のため 180秒 に短縮

# CCXT レート制限対策 
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.15   

MACD_CROSS_PENALTY = 0.08           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.5            
SHORT_TERM_SL_MULTIPLIER = 1.5      

# スコアリングロジック用の定数 
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 45 
RSI_MOMENTUM_HIGH = 55
ADX_TREND_THRESHOLD = 25
BASE_SCORE = 0.55  
VOLUME_CONFIRMATION_MULTIPLIER = 1.5 

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
# UTILITIES & FORMATTING
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
    """スコアと時間軸に基づき推定勝率を算出する"""
    adjusted_rate = 0.50 + (score - 0.50) * 1.5 
    return max(0.40, min(0.80, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (順位表示機能付き)
    """
    
    # 有効なシグナル（エラーやNeutralではない）のみを抽出
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    # 最高の取引シグナル（最もスコアが高く、かつSIGNAL_THRESHOLDを上回っているもの）を取得
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals:
        return "" 
        
    # スコア、RRR、ADX、-ATR、シンボル名の順で最高のシグナルを決定 (main_loopのソートロジックに合わせる)
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
    score = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    entry_type = best_signal.get('entry_type', 'N/A') 

    
    # OKX形式のシンボル (BTC-USDT) を標準形式 (BTC/USDT) に戻して表示
    display_symbol = symbol.replace('-', '/')
    
    # ----------------------------------------------------
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "高 (HIGH)" if score >= 0.75 else "中 (MEDIUM)"
    
    # 順位に応じたヘッダーを動的に生成
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    # リスク幅を計算
    sl_width = abs(entry_price - sl_price)
    
    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 📈 {strength} 発生！ - {direction_emoji}\n" 
        f"==================================\n"
        f"| 🥇 **分析スコア** | <b>{int(score * 100)} / 100 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **TP 到達目安** | {get_tp_reach_time(timeframe)} | (RRR: 1:{rr_ratio:.2f}) |\n"
        f"| 📈 **予測勝率** | {get_estimated_win_rate(score, timeframe) * 100:.1f}% |\n"
        f"==================================\n"
    )

    trade_plan = (
        f"**🎯 推奨取引計画 (ATRベース)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        # ★修正: エントリーを動的に表示
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション ({entry_type}注文) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | 最小リスク距離 |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | 利確 (RRR: 1:{rr_ratio:.2f}) |\n"
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({SHORT_TERM_SL_MULTIPLIER:.1f}xATR) |\n"
        f"----------------------------------\n"
    )
    
    # ----------------------------------------------------
    # 2. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data = s.get('tech_data', {})
        
        score_in_100 = int(s_score * 100)
        
        # 4hトレンドの強調表示
        if tf == '4h':
            long_trend = tech_data.get('long_term_trend', 'Neutral')
            
            # 4h分析の詳細セクション
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend}** ({score_in_100}点)\n"
            )
            
        else:
            # 短期/中期分析の詳細
            # スコアの強弱に応じてアイコンを付与
            score_icon = "🔥" if s_score >= 0.70 else ("📈" if s_score >= 0.60 else "🟡" )
            
            # 長期トレンドとの逆張りペナルティ適用状況
            penalty_status = " (逆張りペナルティ適用)" if tech_data.get('long_term_reversal_penalty') else ""
            
            # モメンタム状態のテキスト
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により取消]"

            # VWAP一致状態のテキスト
            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"

            # Stochastic RSIフィルターの適用状態 (NEW)
            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ STOCHRSI 過熱感により減点: {stoch_penalty:.2f}]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                stoch_k = tech_data.get('stoch_k', 0.0)
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"
                analysis_detail += f"   └ **STOCHRSI (K)**: {stoch_k:.2f}\n"

                # 出来高確証の表示 (NEW)
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高確証**: ✅ {volume_bonus:.2f}pt ボーナス追加 (出来高: {tech_data.get('current_volume', 0):.0f})\n"
                else:
                    analysis_detail += f"   └ **出来高確証**: ❌ 確認なし\n"


    # 3. リスク管理とフッター
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | v12.1.30 - Clear Score Differentiation |\n" # バージョンを更新
        f"==================================\n"
        f"\n<pre>※ このシグナルは高度なテクニカル分析に基づきますが、投資判断は自己責任でお願いします。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


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


async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが未初期化のため、出来高による銘柄更新をスキップします。")
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
        logging.error("CCXTクライアントが初期化されていません。")
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
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

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v12.1.30 - Clear Score Differentiation)
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
        "cci": 0.0, "vwap_consistent": False, "ppo_hist": 0.0, "dc_high": 0.0, "dc_low": 0.0,
        "stoch_k": 50.0, "stoch_d": 50.0, "stoch_filter_penalty": 0.0,
        "volume_confirmation_bonus": 0.0, "current_volume": 0.0, "average_volume": 0.0
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['volume'] = pd.to_numeric(df['volume']) # 出来高も数値化

    # DatetimeIndexを設定してVWAPエラーを解消
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    
    price = df['close'].iloc[-1] if not df.empty else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 

    # 初期設定
    final_side = "Neutral"
    base_score = 0.5
    macd_valid = False
    current_long_term_penalty_applied = False
    
    MACD_HIST_COL = 'MACD_Hist'     
    PPO_HIST_COL = 'PPOh_12_26_9'   
    STOCHRSI_K = 'STOCHRSIk_14_14_3_3'
    STOCHRSI_D = 'STOCHRSId_14_14_3_3'

    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        
        # MACDを個別に計算し、固定の列名を使用
        df['EMA_12'] = ta.ema(df['close'], length=12)
        df['EMA_26'] = ta.ema(df['close'], length=26)
        df['MACD_Line'] = df['EMA_12'] - df['EMA_26']
        df['MACD_Signal'] = ta.ema(df['MACD_Line'], length=9)
        df[MACD_HIST_COL] = df['MACD_Line'] - df['MACD_Signal']
        
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True) # BBM_20_2.0, BBU_20_2.0, BBL_20_2.0 を生成
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        
        # PPO (Percentage Price Oscillator)
        df.ta.ppo(append=True) 
        
        # Donchian Channel (期間20で設定)
        df.ta.donchian(length=20, append=True) # DCL_20, DCU_20 を生成
        
        # Stochastic RSI (STOCHRSI) の計算
        df.ta.stochrsi(append=True)
        
        # ----------------------------------------------------------------------
        # データクリーニング
        # ----------------------------------------------------------------------
        
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', PPO_HIST_COL, STOCHRSI_K, STOCHRSI_D] 
        
        # 必須列にNaNがある行を削除
        df.dropna(subset=required_cols, inplace=True)

        if df.empty:
            return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

        # 2. **動的シグナル判断ロジック (スコアリング)**
        
        # データの安全な取得
        rsi_val = df['rsi'].iloc[-1]
        macd_hist_val = df[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df[MACD_HIST_COL].iloc[-2] 
        adx_val = df['adx'].iloc[-1]
        atr_val = df['atr'].iloc[-1]
        cci_val = df['cci'].iloc[-1] 
        vwap_val = df['vwap'].iloc[-1] 
        ppo_hist_val = df[PPO_HIST_COL].iloc[-1] 
        
        # Stochastic RSI の値を取得
        stoch_k_val = df[STOCHRSI_K].iloc[-1] 
        stoch_d_val = df[STOCHRSI_D].iloc[-1] 

        # 出来高の取得
        current_volume = df['volume'].iloc[-1]
        average_volume = df['volume'].iloc[-31:-1].mean() if len(df) >= 31 else df['volume'].mean()

        
        long_score = 0.5
        short_score = 0.5
        
        # Donchian Channelの値の安全な取得
        dc_low_val = price 
        dc_high_val = price
        dc_cols_present = 'DCL_20' in df.columns and 'DCU_20' in df.columns

        if dc_cols_present:
            dc_low_val = df['DCL_20'].iloc[-1]     
            dc_high_val = df['DCU_20'].iloc[-1]
        
        # A. MACDに基づく方向性
        # ★修正: スコアリングを 0.20 -> 0.25 に強化
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += 0.25 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += 0.25 

        # B. RSIに基づく買われすぎ/売られすぎ (0.10で維持)
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.10
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.10
            
        # C. RSIに基づくモメンタムブレイクアウト (0.10で維持)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.10
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.10

        # D. ADXに基づくトレンドフォロー強化
        # ★修正: スコアリングを 0.05 -> 0.08 に強化
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score:
                long_score += 0.08
            elif short_score > long_score:
                short_score += 0.08
        
        # E. VWAPの一致チェック (0.05で維持)
        vwap_consistent = False
        if price > vwap_val:
            long_score += 0.05
            vwap_consistent = True
        elif price < vwap_val:
            short_score += 0.05
            vwap_consistent = True
        
        # F. PPOに基づくモメンタム強度の評価 (0.05で維持)
        ppo_abs_mean = df[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean:
            long_score += 0.05 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean:
            short_score += 0.05

        # G. Donchian Channelによるブレイクアウト/過熱感フィルター
        is_breaking_high = False
        is_breaking_low = False
        if dc_cols_present: 
            is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
            is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val

            # ★修正: スコアリングを 0.15 -> 0.20 に強化
            if is_breaking_high:
                long_score += 0.20 
            elif is_breaking_low:
                short_score += 0.20
        
        # ★追加: H. 複合モメンタム加速ボーナス
        momentum_bonus = 0.0
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50:
             momentum_bonus = 0.10
             long_score += momentum_bonus
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50:
             momentum_bonus = 0.10
             short_score += momentum_bonus
        
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
        
        # スコアを最終的に 1.0 でキャップ
        score = min(1.0, base_score) 
        
        # ----------------------------------------------------------------------
        # I. Stochastic RSIに基づくエントリー確証/フィルタリング (0.05で維持)
        # ----------------------------------------------------------------------
        stoch_filter_penalty = 0.0
        if timeframe in ['15m', '1h']:
            is_long_confirm = (stoch_k_val < 20 and stoch_d_val < 20) or \
                              (stoch_k_val > stoch_d_val and stoch_k_val < 80)
            
            is_short_confirm = (stoch_k_val > 80 and stoch_d_val > 80) or \
                               (stoch_k_val < stoch_d_val and stoch_k_val > 20)
            
            if side == "ロング" and not is_long_confirm:
                stoch_filter_penalty = 0.05 
            elif side == "ショート" and not is_short_confirm:
                stoch_filter_penalty = 0.05 
                
            score = max(0.5, score - stoch_filter_penalty) 

        # ----------------------------------------------------------------------
        # J. 出来高に基づくシグナル確証 (0.05 * 2 = 0.10で維持)
        # ----------------------------------------------------------------------
        volume_confirmation_bonus = 0.0
        if current_volume > average_volume * VOLUME_CONFIRMATION_MULTIPLIER: 
            
            # 出来高を伴うDCブレイクアウト
            if dc_cols_present and (is_breaking_high or is_breaking_low):
                volume_confirmation_bonus += 0.05
            
            # 出来高を伴う強力なMACDモメンタム
            if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean():
                volume_confirmation_bonus += 0.05
                
            score = min(1.0, score + volume_confirmation_bonus)


        # 3. 4hトレンドフィルターの適用 (15m, 1hのみ)
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
        
        # 4. MACDクロス確認と減点 (モメンタム反転チェック)
        macd_valid = True
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(0.5, score - MACD_CROSS_PENALTY)
                 macd_valid = False
             
        
        # 5. TP/SLとRRRの決定 (ATRに基づく動的計算)
        rr_base = SHORT_TERM_BASE_RRR 
        if (timeframe != '4h') and (side == long_term_trend and long_term_trend != "Neutral"):
            rr_base = SHORT_TERM_MAX_RRR
        
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        
        # ボリンジャーバンドとDonchian Channelのミドル/中央値を取得
        bb_mid = df['BBM_20_2.0'].iloc[-1] if 'BBM_20_2.0' in df.columns else price
        dc_mid = (df['DCU_20'].iloc[-1] + df['DCL_20'].iloc[-1]) / 2 if dc_cols_present else price
        
        entry = price 
        tp1 = 0
        sl = 0
        entry_type = "N/A"

        # エントリーモードの決定
        is_high_conviction = score >= 0.70
        is_strong_trend = adx_val >= 30 
        
        use_market_entry = is_high_conviction or is_strong_trend
        entry_type = "Market" if use_market_entry else "Limit"


        if final_side == "ロング":
            if use_market_entry:
                entry = price
            else:
                optimal_entry = min(bb_mid, dc_mid) 
                entry = min(optimal_entry, price) 
                
                # 乖離が大きすぎる場合の Market Fallback (SL幅の50%を基準)
                if price - entry > sl_dist * 0.5: 
                    entry = price
                    entry_type = "Market (Fallback)"

            # SL/TPは、最終決定されたエントリー価格から計算
            sl = entry - sl_dist
            tp_dist = sl_dist * rr_base 
            tp1 = entry + tp_dist

        elif final_side == "ショート":
            if use_market_entry:
                entry = price
            else:
                optimal_entry = max(bb_mid, dc_mid)
                entry = max(optimal_entry, price) 
                
                # 乖離が大きすぎる場合の Market Fallback (SL幅の50%を基準)
                if entry - price > sl_dist * 0.5: 
                    entry = price
                    entry_type = "Market (Fallback)"
            
            # SL/TPは、最終決定されたエントリー価格から計算
            sl = entry + sl_dist
            tp_dist = sl_dist * rr_base 
            tp1 = entry - tp_dist
            
        else:
            entry_type = "N/A"
            entry, sl, tp1, rr_base = price, 0, 0, 0
        
        # 6. 最終的なサイドの決定
        final_side = side
        # スコアの精度を維持するため、丸めずにそのまま使用
        if score < SIGNAL_THRESHOLD:
             final_side = "Neutral"

        # 7. tech_dataの構築
        bb_width_pct_val = (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / df['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df.columns else 0.0

        tech_data = {
            "rsi": rsi_val,
            "macd_hist": macd_hist_val, 
            "adx": adx_val,
            "bb_width_pct": bb_width_pct_val,
            "atr_value": atr_val,
            "long_term_trend": long_term_trend,
            "long_term_reversal_penalty": current_long_term_penalty_applied,
            "macd_cross_valid": macd_valid,
            "cci": cci_val, 
            "vwap_consistent": vwap_consistent,
            "ppo_hist": ppo_hist_val, 
            "dc_high": dc_high_val,
            "dc_low": dc_low_val,
            "stoch_k": stoch_k_val,
            "stoch_d": stoch_d_val,
            "stoch_filter_penalty": stoch_filter_penalty,
            "volume_confirmation_bonus": volume_confirmation_bonus,
            "current_volume": current_volume,
            "average_volume": average_volume,
        }
        
    except KeyError as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に致命的な Key Error が発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        entry_type = "N/A"

    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        entry_type = "N/A"
        
    # 8. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "side": final_side,
        "score": score, # 精度を維持したスコア
        "confidence": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0,
        "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
        "macro_context": macro_context,
        "client": client_used,
        "timeframe": timeframe,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
        "entry_type": entry_type
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    
    # 0. 4hトレンドの事前計算
    long_term_trend = 'Neutral'
    
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'])
    
    # DatetimeIndexを設定してVWAPエラーを解消
    df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms', utc=True)
    df_4h.set_index('timestamp', inplace=True)
    
    if status_4h == "Success" and len(df_4h) >= LONG_TERM_SMA_LENGTH:
        
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
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend, False) 
    ]
    
    results = await asyncio.gather(*tasks)
    
    # MTF スコアリングブーストロジック
    signal_1h_item = next((r for r in results if r and r.get('timeframe') == '1h'), None)
    signal_15m_item = next((r for r in results if r and r.get('timeframe') == '15m'), None)

    if signal_1h_item and signal_15m_item:
        
        is_1h_strong_signal = signal_1h_item['score'] >= 0.70
        is_direction_matched = signal_1h_item['side'] == signal_15m_item['side']
        
        if is_direction_matched and is_1h_strong_signal:
            # 15m足に0.05のボーナススコアを加算
            signal_15m_item['score'] = min(1.0, signal_15m_item['score'] + 0.05)
            
            
    # 4h分析結果の統合
    for result in results:
        if result and result.get('timeframe') == '4h':
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
            
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            macro_context = await get_crypto_macro_context()
            
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            results_list_of_lists = []
            
            for symbol in monitor_symbols:
                result = await generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME)
                results_list_of_lists.append(result)
                
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
                    # 最初のシグナル、またはより高スコアのシグナルが見つかった場合
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    
                    # 最高のシグナルからタイブレーク用の優位性データを取得
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals,
                        # 順位決定のための補助キー (RRR, ADX, ATR)
                        'rr_ratio': signal.get('rr_ratio', 0.0), 
                        'adx_val': signal.get('tech_data', {}).get('adx', 0.0), 
                        'atr_val': signal.get('tech_data', {}).get('atr_value', 1.0),
                        'symbol': symbol # 最終タイブレークキーとしてシンボル名を追加
                    }
            
            # 複合ソートキーにシンボル名を追加し、完全に一意の順位を生成
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: (
                    x['score'],     # 1. スコア（最も重要、高い方が優位）
                    x['rr_ratio'],  # 2. RRR (高い方が優位)
                    x['adx_val'],   # 3. ADX (高い方が優位)
                    -x['atr_val'],  # 4. ATR (値が小さい方が優位なのでマイナスを付けて降順ソート)
                    x['symbol']     # 5. シンボル名 (アルファベット順に最終決定)
                ), 
                reverse=True
            )
            
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            notify_tasks = [] 
            
            if top_signals_to_notify:
                logging.info(f"🔔 高スコアシグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                # 順位を付与して通知メッセージを生成
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        # 順位 (i + 1) を渡してメッセージ生成
                        msg = format_integrated_analysis_message(symbol, item['all_signals'], i + 1)
                        
                        if msg:
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {i+1}位, スコア: {item['score']:.4f})")
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
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v12.1.30-Clear Score Differentiation (Full Integrated)")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v12.1.30 Startup initializing...") 
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
        "bot_version": "v12.1.30-Clear Score Differentiation (Full Integrated)",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v12.1.30, Clear Score Differentiation)."}, status_code=200)

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
