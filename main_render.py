# ====================================================================================
# Apex BOT v12.1.38 - データクレンジング強化版 (CLEAN_DATA)
# - v12.1.37 をベースに、ufunc 'isfinite' エラー対策としてデータクレンジングを強化:
#   1. OHLCVデータから無限大 (np.inf) を明示的に NaN に変換する処理を追加。
#   2. DatetimeIndexの設定と数値変換の堅牢性を再確認。
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
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS (変更なし)
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
LOOP_INTERVAL = 360        

REQUEST_DELAY_PER_SYMBOL = 0.0

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.80             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.15   
LONG_TERM_ALIGNMENT_BONUS = 0.15

MACD_CROSS_PENALTY = 0.15           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.5            
SHORT_TERM_SL_MULTIPLIER = 1.5      

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 45 
RSI_MOMENTUM_HIGH = 55
ADX_TREND_THRESHOLD = 25
BASE_SCORE = 0.55  

VOLUME_CONFIRMATION_MULTIPLIER = 1.5 

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
# UTILITIES & FORMATTING (変更なし)
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
    return max(0.65, min(0.90, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict]) -> str:
    """3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (ULTRA_HIGH_CONV)"""
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    best_signal = max(valid_signals, key=lambda s: s.get('score', 0.5))
    
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    
    display_symbol = symbol.replace('-', '/')
    
    rank_emoji = "🥇" 
    
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    
    strength = "高 (HIGH)" if score >= 0.75 else "中 (MEDIUM)"
    
    sl_width = abs(entry_price - sl_price)
    
    entry_type = "Entry (Market)"
    if best_signal.get('entry_type') == 'Limit':
        entry_type = "Entry (Limit)"

    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_emoji} **総合 1 位！** 📈 {strength} 発生！ - {direction_emoji}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{get_estimated_win_rate(score, timeframe) * 100:.1f}%</ins>** | **超高確信度** |\n"
        f"| 💯 **分析スコア** | <b>{score * 100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **TP 到達目安** | {get_tp_reach_time(timeframe)} | (RRR: 1:{rr_ratio:.2f}) |\n"
        f"==================================\n"
    )

    trade_plan = (
        f"**🎯 推奨取引計画 (ATRベース)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **{entry_type}** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション ({entry_type.split(' ')[1].replace('(', '').replace(')', '')}注文) |\n"
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | 最小リスク距離 |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | 利確 (RRR: 1:{rr_ratio:.2f}) |\n"
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({SHORT_TERM_SL_MULTIPLIER:.1f}xATR) |\n"
        f"----------------------------------\n"
    )
    
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data = s.get('tech_data', {})
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_trend = tech_data.get('long_term_trend', 'Neutral')
            
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_trend}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.70 else ("📈" if s_score >= 0.60 else "🟡" )
            
            penalty_status = " (逆張りペナルティ適用)" if tech_data.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により取消]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"
            
            stoch_rsi_confirmed = tech_data.get('stoch_rsi_confirmed', False)
            stoch_rsi_text = "[✅ STOCHRSI 確証]" if stoch_rsi_confirmed else "[⚠️ STOCHRSI 不確実]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_rsi_text}\n"
            )
            
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0) * 100
                
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"
                analysis_detail += f"   └ **STOCHRSI (K)**: {tech_data.get('stoch_k', 0.0):.2f}\n"
                if volume_bonus > 0.0:
                    analysis_detail += f"   └ **出来高確証**: ✅ {volume_bonus:.2f}点 ボーナス追加 (出来高: {tech_data.get('current_volume', 0):.0f})\n"

    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | v12.1.38 - CLEAN_DATA |\n" # バージョン更新
        f"==================================\n"
        f"\n<pre>※ このシグナルは高度なテクニカル分析に基づきますが、投資判断は自己責任でお願いします。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION (変更なし)
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
        tickers_swap = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SWAP'})
        
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_swap.items() 
            if symbol.endswith('-USDT') and ticker.get('quoteVolume') is not None
        }

        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        new_monitor_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
            logging.info(f"✅ 出来高TOP{TOP_SYMBOL_LIMIT}銘柄をOKXスワップ市場から更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
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
# CORE ANALYSIS LOGIC (データ前処理を強化)
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (CLEAN_DATA)
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": True, 
        "cci": 0.0, "vwap_consistent": False, "stoch_rsi_confirmed": False, "ppo_hist": 0.0,
        "stoch_k": 50.0, "stoch_d": 50.0, "current_volume": 0, "volume_confirmation_bonus": 0.0
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # ★修正1: DatetimeIndexの設定 (VWAP Fix)
    try:
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): Timestamp処理エラー: {e}")
        return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    # ★修正2: 全てのOHLCVとVolumeを数値型に変換し、NaN/Infを処理 (ufunc Fix 強化)
    for col in ['open', 'high', 'low', 'close', 'volume']:
        # to_numericでエラーを強制し、NaNに変換 (非数値データを除去)
        df[col] = pd.to_numeric(df[col], errors='coerce') 
        
    # **強化ポイント:** 無限大の値をNaNに置き換える (np.isfiniteエラーの最終対策)
    df.replace([np.inf, -np.inf], np.nan, inplace=True) 

    # NaN行の削除 (OHLCV全てが揃っている行のみ残す)
    df.dropna(subset=['open', 'high', 'low', 'close', 'volume'], inplace=True) 
    
    price = df['close'].iloc[-1] if not df.empty else 0.0
    if price == 0.0 or df.empty or len(df) < 30: # 安定性を高めるため最低限のデータ量チェックを強化
        return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    # 初期設定
    final_side = "Neutral"
    macd_valid = True
    stoch_rsi_confirmed = False
    current_long_term_penalty_applied = False
    
    MACD_HIST_COL = 'MACD_Hist'
    PPO_HIST_COL = 'PPOh_12_26_9' 
    DC_HIGH_COL = 'DCH_20'
    DC_LOW_COL = 'DCL_20'

    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        df['EMA_12'] = ta.ema(df['close'], length=12)
        df['EMA_26'] = ta.ema(df['close'], length=26)
        df['MACD_Line'] = df['EMA_12'] - df['EMA_26']
        df['MACD_Signal'] = ta.ema(df['MACD_Line'], length=9)
        df[MACD_HIST_COL] = df['MACD_Line'] - df['MACD_Signal']
        
        df.ta.ppo(close=df['close'], append=True) 
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20)
        # VWAPはここで計算 (DatetimeIndexが設定されている必要あり)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        df.ta.donchian(append=True) 
        df.ta.stochrsi(append=True) 
        
        # データの安全な取得とクリーンアップ (計算後の指標にもNaN/Infチェック)
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', DC_HIGH_COL, DC_LOW_COL, PPO_HIST_COL, 'STOCHRSIk_14_14_3_3']
        
        # 必要な指標がNaNやInfでない行のみに絞る 
        df_filtered = df.copy() 
        for col in required_cols:
             if col in df_filtered.columns:
                 # 計算結果に無限大が含まれていたらNaNに
                 df_filtered[col].replace([np.inf, -np.inf], np.nan, inplace=True)
                 # 確実に数値型に (pandas-taが出力する型が混ざることを防ぐ)
                 df_filtered[col] = pd.to_numeric(df_filtered[col], errors='coerce') 
                 df_filtered.dropna(subset=[col], inplace=True)
        
        if df_filtered.empty or len(df_filtered) < 2:
             logging.warning(f"⚠️ {symbol} ({timeframe}): テクニカル指標の計算結果が不十分です。Neutralとして処理します。")
             raise ValueError("Insufficient data after TA calculation")

        # 2. **動的シグナル判断ロジック (スコアリング)**
        
        # データの安全な取得は、df_filteredから行う
        rsi_val = df_filtered['rsi'].iloc[-1]
        macd_hist_val = df_filtered[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df_filtered[MACD_HIST_COL].iloc[-2] 
        adx_val = df_filtered['adx'].iloc[-1]
        atr_val = df_filtered['atr'].iloc[-1] if np.isfinite(df_filtered['atr'].iloc[-1]) else price * 0.005 
        cci_val = df_filtered['cci'].iloc[-1]
        vwap_val = df_filtered['vwap'].iloc[-1]
        dc_high_val = df_filtered[DC_HIGH_COL].iloc[-1]
        dc_low_val = df_filtered[DC_LOW_COL].iloc[-1]
        ppo_hist_val = df_filtered[PPO_HIST_COL].iloc[-1]
        stoch_k = df_filtered['STOCHRSIk_14_14_3_3'].iloc[-1]
        stoch_d = df_filtered['STOCHRSId_14_14_3_3'].iloc[-1]
        current_volume = df_filtered['volume'].iloc[-1]
        average_volume = df_filtered['volume'].rolling(window=20).mean().iloc[-1] if len(df_filtered) >= 20 else df_filtered['volume'].mean()

        # Score Weighting (ULTRA_HIGH_CONV) - 変更なし
        long_score = 0.5
        short_score = 0.5
        volume_confirmation_bonus = 0.0
        
        # A. MACDに基づく方向性 (寄与度 0.15)
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += 0.15 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += 0.15 

        # B. RSIに基づく買われすぎ/売られすぎ (寄与度 0.10)
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.10
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.10
            
        # C. RSIに基づくモメンタムブレイクアウト (寄与度 0.08)
        if rsi_val > RSI_MOMENTUM_HIGH and df_filtered['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.08
        elif rsi_val < RSI_MOMENTUM_LOW and df_filtered['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.08

        # D. ADXに基づくトレンドフォロー強化 (寄与度 0.05)
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score:
                long_score += 0.05
            elif short_score > long_score:
                short_score += 0.05
        
        # E. VWAPの一致チェック (寄与度 0.05)
        vwap_consistent = False
        if price > vwap_val:
            long_score += 0.05
            vwap_consistent = True
        elif price < vwap_val:
            short_score += 0.05
            vwap_consistent = True
        
        # F. PPOに基づくモメンタム強度の評価 (寄与度 0.03)
        ppo_abs_mean = df_filtered[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean:
            long_score += 0.03 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean:
            short_score += 0.03

        # G. Donchian Channelによるブレイクアウト/過熱感フィルター (寄与度 0.10)
        is_breaking_high = price > dc_high_val and df_filtered['close'].iloc[-2] <= dc_high_val
        is_breaking_low = price < dc_low_val and df_filtered['close'].iloc[-2] >= dc_low_val

        if is_breaking_high:
            long_score += 0.10 
        elif is_breaking_low:
            short_score += 0.10
        
        # H. Stoch RSIに基づくエントリー確証/フィルタリング (寄与度 0.05)
        if stoch_k > stoch_d and stoch_d < 80 and stoch_k > 20: 
             long_score += 0.05
             stoch_rsi_confirmed = True
        elif stoch_k < stoch_d and stoch_d > 20 and stoch_k < 80: 
             short_score += 0.05
             stoch_rsi_confirmed = True
        
        # 最終スコア決定
        if long_score > short_score:
            side = "ロング"
            base_score = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score = short_score
        else:
            side = "Neutral"
            base_score = 0.5
            
        score = base_score

        # I. 出来高に基づくシグナル確証 (Max +0.10)
        if current_volume > average_volume * VOLUME_CONFIRMATION_MULTIPLIER and average_volume > 0: 
            if (is_breaking_high or is_breaking_low):
                volume_confirmation_bonus += 0.05
            if abs(macd_hist_val) > df_filtered[MACD_HIST_COL].abs().mean():
                volume_confirmation_bonus += 0.05
                
            score = min(1.0, score + volume_confirmation_bonus)
        
        # J. 長期トレンドフィルターとボーナスの適用 (15m, 1hのみ)
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Long") or \
               (side == "ショート" and long_term_trend == "Short"):
                score = min(1.0, score + LONG_TERM_ALIGNMENT_BONUS) 
            
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
        
        # K. MACDクロス確認と減点 (モメンタム反転チェック)
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(0.5, score - MACD_CROSS_PENALTY) 
                 macd_valid = False
             
        # 3. TP/SLとRRR、エントリータイプの決定
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        
        # RRRの動的調整
        rr_base = SHORT_TERM_BASE_RRR 
        if (timeframe != '4h') and (side == long_term_trend and long_term_trend != "Neutral"):
            min_adx = 20.0
            max_adx = 40.0
            adx_factor = max(0.0, min(1.0, (adx_val - min_adx) / (max_adx - min_adx)))
            rr_base = SHORT_TERM_BASE_RRR + (SHORT_TERM_MAX_RRR - SHORT_TERM_BASE_RRR) * adx_factor
        
        tp_dist = sl_dist * rr_base 

        # エントリーポイントの決定 
        entry_type = "Market"
        entry = price 
        
        if adx_val < ADX_TREND_THRESHOLD or score < 0.70:
             # Limit価格の候補をVWAPに設定
             limit_price_candidate = vwap_val 

             # 押し目/戻しを待つ
             if side == "ロング" and limit_price_candidate < price:
                 entry = limit_price_candidate
                 entry_type = "Limit"
             elif side == "ショート" and limit_price_candidate > price:
                 entry = limit_price_candidate
                 entry_type = "Limit"
        
             # Limit価格がSLに近すぎる（優位性がない）場合はMarketに戻す
             if entry_type == "Limit" and abs(entry - price) < (sl_dist * 0.5):
                 entry = price
                 entry_type = "Market"

        # TP/SL価格の最終決定 
        if side == "ロング":
            sl = entry - sl_dist
            tp1 = entry + tp_dist
        elif side == "ショート":
            sl = entry + sl_dist
            tp1 = entry - tp_dist
        else:
            entry, sl, tp1, rr_base, entry_type = price, 0, 0, 0, "N/A"
        
        # 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD:
             final_side = "Neutral"

        # 4. tech_dataの構築
        bb_width_pct_val = (df_filtered['BBU_20_2.0'].iloc[-1] - df_filtered['BBL_20_2.0'].iloc[-1]) / df_filtered['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df_filtered.columns else 0.0

        tech_data = {
            "rsi": rsi_val, "macd_hist": macd_hist_val, "adx": adx_val, "bb_width_pct": bb_width_pct_val,
            "atr_value": atr_val, "long_term_trend": long_term_trend, "long_term_reversal_penalty": current_long_term_penalty_applied,
            "macd_cross_valid": macd_valid, "cci": cci_val, "vwap_consistent": vwap_consistent,
            "stoch_rsi_confirmed": stoch_rsi_confirmed, "stoch_k": stoch_k, "stoch_d": stoch_d, 
            "current_volume": current_volume, "volume_confirmation_bonus": volume_confirmation_bonus,
            "ppo_hist": ppo_hist_val, 
        }
        
    except Exception as e:
        # エラーログには原因となった例外を明確に含める
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e}。Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base, entry_type = price, 0, 0, 0, "N/A"
        tech_data = tech_data_defaults 
        
    # 5. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "side": final_side,
        "score": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0,
        "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
        "timeframe": timeframe,
        "tech_data": tech_data,
        "entry_type": entry_type,
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    """3つの時間軸のシグナルを統合して生成する (修正なし)"""
    
    long_term_trend = 'Neutral'
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    if status_4h == "Success" and len(df_4h) >= LONG_TERM_SMA_LENGTH:
        try:
            # 4H足のデータ前処理も安定化のため修正
            df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce')
            df_4h.dropna(subset=['close'], inplace=True)
            df_4h.replace([np.inf, -np.inf], np.nan, inplace=True) # 強化

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
            
    # 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend) 
    ]
    
    results = await asyncio.gather(*tasks)
    
    return [r for r in results if r is not None]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP (notify_tasksの初期化は適切に配置済み)
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
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 全タスクを並列実行, クライアント: {CCXT_CLIENT_NAME})")
            
            tasks = [
                generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME)
                for symbol in monitor_symbols
            ]
            
            results_list_of_lists = await asyncio.gather(*tasks)

            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # 1. 銘柄ごとのベストシグナルを抽出
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
                        'all_signals': all_symbol_signals
                    }
            
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: x['score'], 
                reverse=True
            )
            
            # 2. 超高確信度フィルター (多時間軸一致) の適用
            filtered_high_conviction_signals = []

            for item in sorted_best_signals:
                best_signal = max(item['all_signals'], key=lambda s: s['score']) 
                
                if best_signal['score'] < SIGNAL_THRESHOLD:
                    continue

                base_side = best_signal['side']
                
                signal_15m = next((s for s in item['all_signals'] if s['timeframe'] == '15m'), None)
                
                is_multi_timeframe_confirmed = False

                if best_signal['timeframe'] == '4h':
                    signal_1h = next((s for s in item['all_signals'] if s['timeframe'] == '1h'), None)
                    if signal_1h and signal_15m and \
                       signal_1h['side'] == base_side and \
                       signal_15m['side'] == base_side:
                        is_multi_timeframe_confirmed = True
                        
                elif best_signal['timeframe'] == '1h':
                    if signal_15m and signal_15m['side'] == base_side:
                        is_multi_timeframe_confirmed = True
                
                elif best_signal['timeframe'] == '15m':
                    signal_1h = next((s for s in item['all_signals'] if s['timeframe'] == '1h'), None)
                    if signal_1h and signal_1h['side'] == base_side:
                        is_multi_timeframe_confirmed = True
                
                if best_signal['timeframe'] == '4h' or is_multi_timeframe_confirmed:
                    filtered_high_conviction_signals.append(item)
            
            # 3. 最終通知リストの決定
            top_signals_to_notify = filtered_high_conviction_signals[:TOP_SIGNAL_COUNT]
            
            # -----------------------------------------------------------------
            # 通知実行ロジック
            # -----------------------------------------------------------------
            
            # notify_tasks はここで確実に初期化される
            notify_tasks = [] 

            if top_signals_to_notify:
                logging.info(f"🔔 超高確信度シグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for item in top_signals_to_notify:
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        msg = format_integrated_analysis_message(symbol, item['all_signals'])
                        
                        if msg:
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加 (超高確信度): {log_symbol} (スコア: {item['score']:.4f})")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            
                            task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                            notify_tasks.append(task)
                            
                    else:
                        log_symbol = symbol.replace('-', '/')
                        logging.info(f"🕒 {log_symbol} はクールダウン期間中です。通知をスキップします。")
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            if notify_tasks:
                 # notify_tasksが空でなければ実行
                 await asyncio.gather(*notify_tasks, return_exceptions=True)

            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            # メインループの致命的なエラー処理
            error_name = str(e)
            if 'cannot access local variable' in error_name and 'notify_tasks' in error_name:
                 # notify_tasksは本来定義されているはずだが、もしアクセスエラーが発生した場合の安全策
                 error_name = "NameError: notify_tasks (スコープエラーの再発生 - 暫定的に処理スキップ)"
                 pass # エラーをログに出力後、次のループへ
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP (バージョン表記のみ更新)
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v12.1.38-CLEAN_DATA")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v12.1.38 Startup initializing...") 
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
        "bot_version": "v12.1.38-CLEAN_DATA",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v12.1.38, CLEAN_DATA)."}, status_code=200)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    if sys.platform.startswith('win') or sys.platform.startswith('darwin'):
        try:
            import selectors
            if selectors.DefaultSelector is selectors.SelectSelector:
                selectors.DefaultSelector = selectors.SelectSelector
        except Exception:
            pass
            
    uvicorn.run(app, host="0.0.0.0", port=port)
