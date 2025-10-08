# ====================================================================================
# Apex BOT v16.0.0 - Adaptive EAS & Dynamic Risk Adjustment
# - 資金調達率 (Funding Rate) を取得し、レバレッジの偏り（需給バイアス）をスコアに反映 (+/- 0.08点)
# - 固定TP/RRRを廃止し、ATRに基づく動的トレーリングストップ (DTS) を採用し、利益最大化を狙う
# - スコア条件はv14.0.0の厳格な設定 (SIGNAL_THRESHOLD=0.75, BASE_SCORE=0.40) を維持
# - NEW: Limit Entryの優位性をEntry Advantage Score (EAS)で評価し、最も効率の良い底/天井を通知する
# - IMPROVED: ボラティリティに応じてATR乗数を動的に調整 (ATR_TRAIL_MULTIPLIER)
# - IMPROVED: EAS優位性が低い場合、Limit EntryからMarket Entryへ自動フォールバック (約定率向上)
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
# CONFIG & CONSTANTS (v16.0.0 改良)
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 監視シンボル設定
DEFAULT_MONITOR_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "DOGE/USDT", "LTC/USDT", "BCH/USDT", "LINK/USDT", "DOT/USDT", "ADA/USDT"]
ADDITIONAL_SYMBOLS_FOR_VOLUME_CHECK = ["BTC/USDT", "ETH/USDT"] # FGI Proxy用
CURRENT_MONITOR_SYMBOLS = [] # 実行時に設定

# CCXT設定
CCXT_CLIENT_NAME = os.environ.get("CCXT_CLIENT_NAME", "binance") # binance, bybit, etc.
CCXT_API_KEY = os.environ.get("CCXT_API_KEY")
CCXT_SECRET = os.environ.get("CCXT_SECRET")
# 実行環境設定
DEBUG_MODE = os.environ.get("DEBUG_MODE", "False").lower() == "true"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# 分析時間足 (ロングタームトレンド, DTS/SL, エントリーモメンタム)
TIMEFRAMES = ["15m", "1h", "4h"] 
MAIN_TIMEFRAME = "15m"

# シグナルとスコアリング
BASE_SCORE = 0.40
SIGNAL_THRESHOLD = 0.75 # シグナル確定に必要な最低スコア
TOP_SIGNAL_COUNT = 3 # 通知する上位シグナル数

# MACD/RSI/ADXスコア配点
MACD_SCORE = 0.15
RSI_SCORE_OVERBOUGHT = 0.08
RSI_SCORE_MOMENTUM = 0.12
ADX_SCORE = 0.10
VWAP_SCORE = 0.04
PPO_SCORE = 0.04
DC_SCORE = 0.15
COMPOSITE_MOMENTUM_SCORE = 0.05
STRUCTURAL_SCORE = 0.07
VOLUME_CONFIRMATION_SCORE = 0.12
FR_BIAS_SCORE = 0.08
FGI_PROXY_SCORE = 0.07

# フィルターペナルティ
LONG_TERM_PENALTY = 0.20 # 4hトレンドに逆行する場合
MACD_CROSS_PENALTY = 0.15 # モメンタムが反転する場合

# Dynamic Trailing Stop (DTS) Parameters
DTS_RRR_DISPLAY = 5.0 # 動的決済採用時、メッセージで表示する目標RRR

# NEW: Volatility Adaptive Risk Parameters
# ATR乗数（SL幅）の最小/最大値を設定
ATR_MULTIPLIER_MIN = 2.5
ATR_MULTIPLIER_MAX = 4.0
# ボラティリティ調整の基準 (BB Width %)。5.0%以上で乗数を下げ、2.0%以下で乗数を上げる
VOLATILITY_HIGH_THRESHOLD = 5.0 
VOLATILITY_LOW_THRESHOLD = 2.0  

# NEW: Adaptive Entry Advantage Score (EAS) Parameters
# Limit Entryが採用されるEASの最低基準 (ATR 0.5倍の優位性)
EAS_MIN_ADVANTAGE_THRESHOLD = 0.5 

# グローバル変数
EXCHANGE_CLIENT = None
LAST_SUCCESS_TIME = 0
LAST_ANALYSIS_SIGNALS = []
LAST_NOTIFIED_HASH = ""
LAST_SIGNAL_MESSAGE = ""

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ====================================================================================
# UTILITY FUNCTIONS
# ====================================================================================

def get_ccxt_client(client_name: str):
    """CCXTクライアントを初期化または取得する"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        return EXCHANGE_CLIENT
    
    if client_name in ccxt.exchanges:
        exchange_class = getattr(ccxt_async, client_name)
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': CCXT_API_KEY,
            'secret': CCXT_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', # 先物取引をデフォルトとする
            },
        })
        logging.info(f"{client_name.upper()} クライアントを初期化しました。")
        return EXCHANGE_CLIENT
    else:
        logging.error(f"対応していない取引所クライアント名: {client_name}")
        sys.exit(1)

async def fetch_markets():
    """取引所から全てのシンボルを取得し、取引量の多いものをフィルタリングする"""
    client = get_ccxt_client(CCXT_CLIENT_NAME)
    try:
        # 市場データをフェッチ
        markets = await client.fetch_markets()
        
        # USDTペアかつ先物/マージン取引可能なシンボルを抽出
        futures_markets = [
            m['symbol'] for m in markets 
            if '/USDT' in m['symbol'] and m['active'] and m.get('contract', True) and m.get('spot', False) is False
        ]
        
        # トップ取引量のシンボルを取得 (ここでは静的リストに頼る)
        global CURRENT_MONITOR_SYMBOLS
        CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_MONITOR_SYMBOLS) & set(futures_markets))
        logging.info(f"監視シンボル: {CURRENT_MONITOR_SYMBOLS}")
        
    except Exception as e:
        logging.error(f"市場データの取得中にエラーが発生しました: {e}")
        # エラー発生時はデフォルトシンボルで続行
        CURRENT_MONITOR_SYMBOLS = DEFAULT_MONITOR_SYMBOLS

def format_price_utility(price: float, symbol: str) -> str:
    """価格を指定シンボルに応じて整形する"""
    if price >= 100:
        return f"{price:,.2f}"
    elif price >= 1:
        return f"{price:,.4f}"
    elif price >= 0.01:
        return f"{price:,.6f}"
    else:
        return f"{price:,.8f}"

def get_sentiment_from_btc_trend(btc_trend: str) -> str:
    """BTCの長期トレンドから市場センチメントを推定する"""
    if btc_trend == 'Long':
        return 'Risk-On'
    elif btc_trend == 'Short':
        return 'Risk-Off'
    else:
        return 'Neutral'

# ====================================================================================
# TELEGRAM NOTIFICATION
# ====================================================================================

def send_telegram_message(message: str):
    """Telegramへメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # logging.warning("Telegram設定がされていません。")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegramメッセージの送信に失敗しました: {e}")

# ====================================================================================
# DATA FETCHING & TECHNICAL ANALYSIS
# ====================================================================================

async def fetch_ohlcv(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得する"""
    client = get_ccxt_client(CCXT_CLIENT_NAME)
    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=300)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        return df
    except Exception as e:
        logging.error(f"{symbol} {timeframe} のOHLCV取得中にエラー: {e}")
        return None

async def fetch_funding_rate(symbol: str) -> float:
    """最新の資金調達率を取得する"""
    client = get_ccxt_client(CCXT_CLIENT_NAME)
    try:
        # ccxtのFunding Rate取得は取引所によって異なる
        if hasattr(client, 'fetch_funding_rate'):
            funding_rate_data = await client.fetch_funding_rate(symbol)
            return funding_rate_data['fundingRate']
        else:
            return 0.0 # サポートされていない場合は0として処理
    except Exception as e:
        logging.debug(f"{symbol} のFunding Rate取得中にエラー: {e}")
        return 0.0

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """主要なテクニカル指標を計算する"""
    
    # 出来高 (ATR計算に必要)
    df['tr'] = ta.true_range(df['high'], df['low'], df['close'])
    df['atr'] = ta.sma(df['tr'], length=14)
    
    # MACD
    macd_data = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9']
    df['MACDs'] = macd_data['MACDs_12_26_9']
    
    # RSI
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # ADX/DMI
    dmi_data = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['ADX'] = dmi_data['ADX_14']
    df['DMP'] = dmi_data['DMP_14']
    df['DMN'] = dmi_data['DMN_14']
    
    # VWAP (簡易SMAとして計算)
    df['SMA_50'] = ta.sma(df['close'], length=50) # 長期トレンド判定に利用
    df['VWAP_20'] = ta.sma(df['close'], length=20) # 簡易的なVWAP proxyとして利用
    
    # PPO (Percentage Price Oscillator)
    ppo_data = ta.ppo(df['close'], fast=12, slow=26, signal=9)
    df['PPO'] = ppo_data['PPO_12_26_9']
    
    # Bollinger Bands (ボラティリティ測定用)
    bb_data = ta.bbands(df['close'], length=20, std=2.0)
    df['BBU_20_2.0'] = bb_data['BBU_20_2.0']
    df['BBL_20_2.0'] = bb_data['BBL_20_2.0']
    
    # Donchian Channel
    dc_data = ta.donchian(df['high'], df['low'], lower_length=20, upper_length=20)
    df['DCL'] = dc_data['DCL_20']
    df['DCU'] = dc_data['DCU_20']
    
    # Fibonacci Pivot Point (Structural Support/Resistance)
    # 簡易計算: 前日のHLCを使用
    df['Pivot_H'] = df['high'].shift(1)
    df['Pivot_L'] = df['low'].shift(1)
    df['Pivot_C'] = df['close'].shift(1)
    # S3 = L - 2 * (H - C)
    df['Pivot_S3'] = df['Pivot_L'] - 2 * (df['Pivot_H'] - df['Pivot_C'])
    # R3 = H + 2 * (C - L)
    df['Pivot_R3'] = df['Pivot_H'] + 2 * (df['Pivot_C'] - df['Pivot_L'])

    return df.dropna()

# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """単一時間足での分析とスコアリングを実行する"""
    
    df = await fetch_ohlcv(symbol, timeframe)
    if df is None or df.empty:
        return None
    
    df = calculate_indicators(df)
    if df.empty:
        return None

    # 最新バーのデータを取得
    current_data = df.iloc[-1]
    prev_data = df.iloc[-2]
    
    price = current_data['close']
    atr_val = current_data['atr']
    
    # 1. BASE SCOREとMACRO CONTEXT
    score = BASE_SCORE
    side = "Neutral"
    
    # 4Hトレンドに基づくFGI Proxyのボーナス/ペナルティ
    fgi_context = macro_context.get('fgi_proxy_sentiment', 'Neutral')
    fgi_trend_bonus = 0.0
    if long_term_trend == 'Long' and fgi_context == 'Risk-On':
        fgi_trend_bonus += FGI_PROXY_SCORE
    elif long_term_trend == 'Short' and fgi_context == 'Risk-Off':
        fgi_trend_bonus += FGI_PROXY_SCORE
    elif long_term_trend == 'Short' and fgi_context == 'Risk-On':
        fgi_trend_bonus -= FGI_PROXY_SCORE
    elif long_term_trend == 'Long' and fgi_context == 'Risk-Off':
        fgi_trend_bonus -= FGI_PROXY_SCORE
    score += fgi_trend_bonus

    # 2. LONG / SHORT の決定
    # MACD, RSI, ADX, PPO, VWAPの統合で方向性を決定

    # Long Bias Check
    long_bias = 0
    if current_data['MACDh'] > 0 and current_data['MACDh'] > prev_data['MACDh']: long_bias += 1 # MACD加速
    if current_data['RSI'] > 50: long_bias += 1
    if current_data['close'] > current_data['VWAP_20']: long_bias += 1
    if current_data['PPO'] > 0: long_bias += 1
    
    # Short Bias Check
    short_bias = 0
    if current_data['MACDh'] < 0 and current_data['MACDh'] < prev_data['MACDh']: short_bias += 1 # MACD加速
    if current_data['RSI'] < 50: short_bias += 1
    if current_data['close'] < current_data['VWAP_20']: short_bias += 1
    if current_data['PPO'] < 0: short_bias += 1

    if long_bias >= 3 and short_bias == 0:
        side = "Long"
    elif short_bias >= 3 and long_bias == 0:
        side = "Short"
    else:
        return None # 方向性が定まらない

    # 3. スコアリング
    
    # MACD Score: 方向と加速
    if side == "Long" and current_data['MACDh'] > 0 and current_data['MACDh'] > prev_data['MACDh']: score += MACD_SCORE
    if side == "Short" and current_data['MACDh'] < 0 and current_data['MACDh'] < prev_data['MACDh']: score += MACD_SCORE
        
    # RSI Score: 買われすぎ/売られすぎ
    if side == "Long" and current_data['RSI'] < 30: score += RSI_SCORE_OVERBOUGHT
    if side == "Short" and current_data['RSI'] > 70: score += RSI_SCORE_OVERBOUGHT
        
    # RSI Score: モメンタムブレイクアウト
    if side == "Long" and prev_data['RSI'] < 50 and current_data['RSI'] >= 50: score += RSI_SCORE_MOMENTUM
    if side == "Short" and prev_data['RSI'] > 50 and current_data['RSI'] <= 50: score += RSI_SCORE_MOMENTUM
        
    # ADX Score: トレンド強度
    if current_data['ADX'] >= 30: score += ADX_SCORE
        
    # VWAP Score: 価格とVWAPの関係
    if side == "Long" and current_data['close'] > current_data['VWAP_20']: score += VWAP_SCORE
    if side == "Short" and current_data['close'] < current_data['VWAP_20']: score += VWAP_SCORE
        
    # PPO Score: 平均からの乖離
    if side == "Long" and current_data['PPO'] > 0: score += PPO_SCORE
    if side == "Short" and current_data['PPO'] < 0: score += PPO_SCORE

    # DC Score: レンジブレイクアウト
    is_dc_breakout = False
    if side == "Long" and current_data['close'] > current_data['DCU']: 
        score += DC_SCORE
        is_dc_breakout = True
    if side == "Short" and current_data['close'] < current_data['DCL']: 
        score += DC_SCORE
        is_dc_breakout = True
        
    # 複合モメンタム: MACDh, PPO, RSIが同一方向で加速
    if side == "Long" and current_data['MACDh'] > prev_data['MACDh'] and current_data['PPO'] > prev_data['PPO'] and current_data['RSI'] > prev_data['RSI']:
        score += COMPOSITE_MOMENTUM_SCORE
    if side == "Short" and current_data['MACDh'] < prev_data['MACDh'] and current_data['PPO'] < prev_data['PPO'] and current_data['RSI'] < prev_data['RSI']:
        score += COMPOSITE_MOMENTUM_SCORE
        
    # 出来高確証 (ブレイクアウト時のみ)
    if is_dc_breakout and current_data['volume'] >= df['volume'].rolling(window=20).mean().iloc[-1] * 2.5:
        score += VOLUME_CONFIRMATION_SCORE
        
    # Funding Rate Bias Filter
    funding_rate = macro_context.get('funding_rate', 0.0)
    FR_THRESHOLD = 0.00015 # 0.015%
    if side == "Long" and funding_rate < -FR_THRESHOLD: score += FR_BIAS_SCORE # 逆張り
    if side == "Short" and funding_rate > FR_THRESHOLD: score += FR_BIAS_SCORE # 逆張り
    if side == "Long" and funding_rate > FR_THRESHOLD: score -= FR_BIAS_SCORE # 順張りで過熱
    if side == "Short" and funding_rate < -FR_THRESHOLD: score -= FR_BIAS_SCORE # 順張りで過熱
        
    # 4. フィルター適用
    
    # 4Hトレンドとの逆行ペナルティ
    if not long_term_penalty_applied and (side == "Long" and long_term_trend == "Short") or (side == "Short" and long_term_trend == "Long"):
        score -= LONG_TERM_PENALTY
        long_term_penalty_applied = True
        
    # MACDクロスによるモメンタム反転ペナルティ
    if side == "Long" and prev_data['MACD'] < prev_data['MACDs'] and current_data['MACD'] < current_data['MACDs']:
        score -= MACD_CROSS_PENALTY
    if side == "Short" and prev_data['MACD'] > prev_data['MACDs'] and current_data['MACD'] > current_data['MACDs']:
        score -= MACD_CROSS_PENALTY

    # 5. TP/SLとRRRの決定 (Dynamic Trailing Stop & Structural SL)
    
    # 💡 弱点解消 1: ATR乗数の動的調整
    bb_width_pct_val = (current_data['BBU_20_2.0'] - current_data['BBL_20_2.0']) / price * 100
    current_atr_multiplier = 3.0 # 中間値
    
    if bb_width_pct_val >= VOLATILITY_HIGH_THRESHOLD:
        current_atr_multiplier = ATR_MULTIPLIER_MIN
    elif bb_width_pct_val <= VOLATILITY_LOW_THRESHOLD:
        current_atr_multiplier = ATR_MULTIPLIER_MAX
    else:
        # 中間ボラティリティの場合は線形補間（ここではシンプルに3.0を維持）
        pass

    # SL Dist (ATRに基づく初期追跡ストップの距離)
    sl_dist_atr = atr_val * current_atr_multiplier 
    
    # Structural/Pivot S/R の定義
    pivot_s3 = current_data['Pivot_S3']
    pivot_r3 = current_data['Pivot_R3']
    structural_sl_used = False
    
    if side == "Long":
        # エントリー: 現在価格を仮とする
        entry = price 
        
        # SL候補1: ATRベース
        sl_atr = entry - sl_dist_atr
        # SL候補2: Structural/Pivot S3
        sl_structural = pivot_s3
        
        # Structural SLがタイトで優位性がある場合（ATR SLより上）に採用
        if sl_structural > sl_atr and (entry - sl_structural) > 0.5 * atr_val: # 最低ATR 0.5倍の幅を確保
            sl_price = sl_structural
            sl_dist_final = entry - sl_price
            sl_source = "Structural"
            structural_sl_used = True
        else:
            sl_price = sl_atr
            sl_dist_final = sl_dist_atr
            sl_source = "ATR"
        
        # TP: DTS採用のため、メッセージ表示用としてSL幅のRRR_DISPLAY倍を設定
        tp_price = entry + sl_dist_final * DTS_RRR_DISPLAY
        
    elif side == "Short":
        entry = price
        
        # SL候補1: ATRベース
        sl_atr = entry + sl_dist_atr
        # SL候補2: Structural/Pivot R3
        sl_structural = pivot_r3
        
        # Structural SLがタイトで優位性がある場合（ATR SLより下）に採用
        if sl_structural < sl_atr and (sl_structural - entry) * -1 > 0.5 * atr_val: # 最低ATR 0.5倍の幅を確保
            sl_price = sl_structural
            sl_dist_final = sl_price - entry
            sl_source = "Structural"
            structural_sl_used = True
        else:
            sl_price = sl_atr
            sl_dist_final = sl_dist_atr
            sl_source = "ATR"
            
        tp_price = entry - sl_dist_final * DTS_RRR_DISPLAY
        
    else:
        return None

    # RRR (メッセージ表示用)
    rr_ratio = DTS_RRR_DISPLAY 
    
    # 6. Entry Advantage Score (EAS) と Limit Entryの決定
    
    entry_type = "Market" # デフォルトはMarket Entry
    limit_entry_price = entry # 初期値は現在価格
    entry_advantage_score = 0.0 # ATR換算の優位性

    # スコアが厳格ではない (0.75以上0.80未満) または トレンドが強くない (ADX < 35) 場合、Limit Entryを検討
    if (score < 0.80 and score >= SIGNAL_THRESHOLD) or current_data['ADX'] < 35:
        # Limit Entryの価格を設定
        if side == "Long":
            # 押し目買い: 1 ATR分の価格優位性を狙う（例として）
            limit_entry_price = price - atr_val 
            # Limit EntryがSLを内包しないかチェック（Limit価格がSLより上にあるか）
            if limit_entry_price > sl_price:
                 entry_type = "Limit"
                 entry_advantage_score = (price - limit_entry_price) / atr_val
            else:
                 # Limit価格がSLに近すぎる場合はMarketを維持
                 limit_entry_price = price 

        elif side == "Short":
            # 戻り売り: 1 ATR分の価格優位性を狙う
            limit_entry_price = price + atr_val 
            # Limit EntryがSLを内包しないかチェック（Limit価格がSLより下にあるか）
            if limit_entry_price < sl_price:
                 entry_type = "Limit"
                 entry_advantage_score = (limit_entry_price - price) / atr_val * -1 # Shortは優位性がマイナス値になるように調整
            else:
                 limit_entry_price = price

    # Limit Entryが採用された場合、エントリ価格とSLを再計算
    if entry_type == "Limit":
        # Limit Entry価格をエントリーとして設定
        entry = limit_entry_price
        
        # SLはLimit Entry価格を基準に再計算（構造的SLの有無を再度確認）
        if side == "Long":
            # SL候補1: ATRベース (LimitからATR乗数分)
            sl_atr_recalc = entry - sl_dist_atr
            if sl_structural > sl_atr_recalc and (entry - sl_structural) > 0.5 * atr_val:
                sl_price = sl_structural
            else:
                sl_price = sl_atr_recalc
            
        elif side == "Short":
            sl_atr_recalc = entry + sl_dist_atr
            if sl_structural < sl_atr_recalc and (sl_structural - entry) * -1 > 0.5 * atr_val:
                sl_price = sl_structural
            else:
                sl_price = sl_atr_recalc
        
        # リスク幅を再計算
        sl_dist_final = abs(entry - sl_price)
        tp_price = entry + sl_dist_final * DTS_RRR_DISPLAY if side == "Long" else entry - sl_dist_final * DTS_RRR_DISPLAY
        rr_ratio = DTS_RRR_DISPLAY

    # 7. tech_dataの構築
    regime = "Trend" if current_data['ADX'] >= 30 else "Range"
    tech_data = {
        "side": side,
        "price": price,
        "entry": entry,
        "sl": sl_price,
        "tp": tp_price,
        "rr_ratio": rr_ratio,
        "atr_val": atr_val,
        "long_term_trend": long_term_trend,
        "regime": regime,
        "macd_h": current_data['MACDh'],
        "rsi": current_data['RSI'],
        "adx": current_data['ADX'],
        "fr": funding_rate,
        "bb_width_pct": bb_width_pct_val,
        "dynamic_atr_multiplier": current_atr_multiplier, # NEW: 使用したATR乗数を記録
        "dynamic_exit_strategy": "DTS" 
    }

    # 8. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "timeframe": timeframe,
        "side": side,
        "score": score,
        "entry": entry,
        "sl": sl_price,
        "tp": tp_price,
        "price": price, # 現在価格 (Market Entryフォールバックに必要)
        "rr_ratio": rr_ratio,
        "entry_type": entry_type,
        "entry_advantage_score": entry_advantage_score,
        "long_term_penalty_applied": long_term_penalty_applied,
        "dynamic_atr_multiplier": current_atr_multiplier, # NEW: 信号に含める
        "tech_data": tech_data,
        "sl_source": sl_source
    }
    
    return signal_candidate

# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """メインのBOT実行ロジック"""
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, LAST_NOTIFIED_HASH, LAST_SIGNAL_MESSAGE
    
    # 1. 初期化と市場データのフェッチ
    await fetch_markets()
    client = get_ccxt_client(CCXT_CLIENT_NAME)

    while True:
        start_time = time.time()
        try:
            # 2. マクロコンテキストの取得 (4Hトレンド, Funding Rate)
            macro_context = {}
            btc_ohlcv = await fetch_ohlcv("BTC/USDT", "4h")
            eth_ohlcv = await fetch_ohlcv("ETH/USDT", "4h")
            
            # BTC 4h トレンド (50SMA)
            btc_trend_4h = "Neutral"
            if btc_ohlcv is not None and not btc_ohlcv.empty:
                btc_ohlcv = calculate_indicators(btc_ohlcv)
                if btc_ohlcv.iloc[-1]['close'] > btc_ohlcv.iloc[-1]['SMA_50']:
                    btc_trend_4h = "Long"
                elif btc_ohlcv.iloc[-1]['close'] < btc_ohlcv.iloc[-1]['SMA_50']:
                    btc_trend_4h = "Short"
            
            # FGI Proxy Sentiment (BTC/ETH 4hトレンドの統合)
            eth_trend_4h = "Neutral"
            if eth_ohlcv is not None and not eth_ohlcv.empty:
                eth_ohlcv = calculate_indicators(eth_ohlcv)
                if eth_ohlcv.iloc[-1]['close'] > eth_ohlcv.iloc[-1]['SMA_50']:
                    eth_trend_4h = "Long"
                elif eth_ohlcv.iloc[-1]['close'] < eth_ohlcv.iloc[-1]['SMA_50']:
                    eth_trend_4h = "Short"
                    
            fgi_proxy_sentiment = "Neutral"
            if btc_trend_4h == "Long" and eth_trend_4h == "Long":
                 fgi_proxy_sentiment = "Risk-On"
            elif btc_trend_4h == "Short" and eth_trend_4h == "Short":
                 fgi_proxy_sentiment = "Risk-Off"
                 
            macro_context['btc_trend_4h'] = btc_trend_4h
            macro_context['fgi_proxy_sentiment'] = fgi_proxy_sentiment
            
            # Funding Rate (ランダムに1シンボルを取得)
            random_symbol = random.choice(CURRENT_MONITOR_SYMBOLS)
            fr = await fetch_funding_rate(random_symbol)
            macro_context['funding_rate'] = fr

            # 3. 全てのシンボルと時間足で分析を実行
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                for tf in TIMEFRAMES:
                    # 4hはトレンド判定にのみ使用
                    if tf == '4h' and symbol != "BTC/USDT" and symbol != "ETH/USDT":
                        continue
                        
                    # 15mと1hの分析は、4hトレンドを考慮
                    long_term_trend = btc_trend_4h # 基本はBTCの4hトレンドを使用
                    long_term_penalty_applied = False
                    
                    if tf in ["15m", "1h"]:
                        analysis_tasks.append(
                            analyze_single_timeframe(
                                symbol, tf, macro_context, CCXT_CLIENT_NAME, long_term_trend, long_term_penalty_applied
                            )
                        )

            all_signals = await asyncio.gather(*analysis_tasks)
            all_signals = [s for s in all_signals if s is not None]

            # 4. シグナルの集約とフィルタリング
            best_signals_per_symbol = {}
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    best_signals_per_symbol[symbol] = {
                        'symbol': symbol,
                        'score': score,
                        'rr_ratio': signal['rr_ratio'],
                        'entry_type': signal['entry_type'],
                        'entry_advantage_score': signal['entry_advantage_score'],
                        'all_signals': [signal]
                    }
                elif score == best_signals_per_symbol[symbol]['score']:
                    best_signals_per_symbol[symbol]['all_signals'].append(signal)

            # --- Limit Entry ポジションのみをフィルタリングし、ソート基準を優位性スコアに変更 ---
            all_eligible_signals = []
            
            for symbol, item in best_signals_per_symbol.items():
                signal = item['all_signals'][0] # ベストスコアの信号
                score = item['score']
                
                if score < SIGNAL_THRESHOLD:
                    continue
                    
                entry_type = item['entry_type']
                eas = item['entry_advantage_score']

                # 💡 弱点解消 2: EAS閾値の動的化とMarket Entryへのフォールバック
                # EASが設定閾値未満の場合、Market Entryに強制フォールバック
                if entry_type == 'Limit' and eas < EAS_MIN_ADVANTAGE_THRESHOLD:
                     entry_type = 'Market'
                     item['entry_type'] = 'Market' # Itemを更新
                     item['all_signals'][0]['entry_type'] = 'Market'
                     item['entry_advantage_score'] = 0.0 # EASはリセット
                     # Entry価格を現在価格に設定し直す
                     item['all_signals'][0]['entry'] = item['all_signals'][0]['price'] 
                     
                     logging.info(f"💡 {symbol} のEAS({eas:.2f})が低いため、Limit -> Market Entryにフォールバックしました。")

                all_eligible_signals.append(item)

            # ソート: Entry Advantage Score (EAS, 優位性) を最優先、次にスコアの順でソート
            sorted_best_signals = sorted(
                all_eligible_signals, 
                key=lambda x: (
                    x['entry_advantage_score'] if x['entry_type'] == 'Limit' else x['score'] * 0.001, # Limitを優位性で優先。Marketはスコアを低めに評価しLimitより後回しにする
                    x['score'],     
                    x['rr_ratio'],  
                    x['symbol']     
                ), 
                reverse=True
            )
            # --------------------------------------------------------------------------
                    
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            LAST_ANALYSIS_SIGNALS = top_signals_to_notify
            LAST_SUCCESS_TIME = time.time()

            # 5. メッセージ生成と通知
            
            if top_signals_to_notify:
                message_parts = []
                for i, item in enumerate(top_signals_to_notify):
                    # ベストスコアの信号（フォールバック後の情報）を使用
                    best_signal = item['all_signals'][0] 
                    message_parts.append(format_integrated_analysis_message(item['symbol'], [best_signal], i + 1))
                
                final_message = "\n\n".join(message_parts)
                current_hash = hash(final_message)

                if current_hash != LAST_NOTIFIED_HASH or DEBUG_MODE:
                    send_telegram_message(final_message)
                    LAST_NOTIFIED_HASH = current_hash
                    LAST_SIGNAL_MESSAGE = final_message
                    logging.info(f"✅ {len(top_signals_to_notify)} 件の新規/更新シグナルを通知しました。")
                else:
                    logging.info("📝 シグナルは前回から変化がありません。通知をスキップします。")
            else:
                if time.time() - LAST_SUCCESS_TIME > 3600 * 4: # 4時間何も通知がない場合
                    no_signal_message = f"🚨 {datetime.now(JST).strftime('%H:%M')} 現在、エントリー閾値 ({SIGNAL_THRESHOLD}) を超えるシグナルはありません。市場はレンジまたは優位性の低い状況です。\n⚙️ BOT Ver: v16.0.0"
                    if LAST_SIGNAL_MESSAGE != no_signal_message:
                        send_telegram_message(no_signal_message)
                        LAST_SIGNAL_MESSAGE = no_signal_message
                logging.info("❌ エントリー閾値を超えるシグナルはありません。")

            # 6. 次の実行までの待機
            elapsed_time = time.time() - start_time
            sleep_time = max(0, 60 - elapsed_time) # 1分毎に実行
            await asyncio.sleep(sleep_time)

        except (ccxt.errors.RequestTimeout, ccxt.errors.ExchangeError) as e:
            error_name = type(e).__name__
            logging.error(f"CCXTエラーが発生しました: {error_name}")
            await asyncio.sleep(30)
        except Exception as e:
            error_name = type(e).__name__
            # デバッグモードでない場合はエラーログのみ
            if DEBUG_MODE:
                logging.exception(f"メインループで致命的なエラー: {error_name}")
            else:
               pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# NOTIFICATION MESSAGE FORMATTING
# ====================================================================================

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """最終的な通知メッセージを整形する"""
    
    # スコアで並べ替え、最もスコアの高い信号を使用
    best_signal = max(
        signals,
        key=lambda x: x['score']
    )
    
    # 主要な取引情報を抽出
    side = best_signal['side']
    score = best_signal['score']
    timeframe = best_signal['timeframe']
    entry_price = best_signal['entry']
    sl_price = best_signal['sl']
    tp_price = best_signal['tp']
    rr_ratio = best_signal['rr_ratio']
    entry_type = best_signal.get('entry_type', 'N/A') 
    sl_source = best_signal.get('sl_source', 'ATR')
    dynamic_atr_multiplier = best_signal.get('dynamic_atr_multiplier', 3.0) # NEW: 動的乗数を取得

    # 統合分析のサマリー
    tech_data = best_signal.get('tech_data', {})
    regime = tech_data.get('regime', 'N/A')
    long_term_trend = tech_data.get('long_term_trend', 'N/A')
    
    # 表示用シンボル名
    display_symbol = symbol.replace('/', ' ')

    # SL Source文字列
    sl_source_str = "構造的S/R" if sl_source == 'Structural' else "ボラティリティ"
    
    # 決済戦略の表示をDTSに変更
    exit_type_str = "DTS (動的追跡損切)" 
    
    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"| 🏆 **RANK** | **{rank}** / {TOP_SIGNAL_COUNT} | 📊 **SCORE** | **{score:.3f}** / 1.000 |\n"
        f"| 🕰️ **時間足** | **{timeframe}** | 🧭 **方向性** | **{side.upper()}** |\n"
        f"| ⏰ **決済戦略** | **{exit_type_str}** | (目標RRR: 1:{DTS_RRR_DISPLAY:.2f}+) |\n" 
        f"==================================\n"
    )

    # リスク幅を計算 (初期のストップ位置との差)
    sl_width = abs(entry_price - sl_price)

    # 取引計画の表示をDTSに合わせて変更
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic Trailing Stop & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | **{side}**ポジション (**<ins>{'市場価格注文' if entry_type == 'Market' else '底/天井を狙う Limit 注文'}</ins>**) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | **初動リスク** ({sl_source_str} / **ATR x {dynamic_atr_multiplier:.1f}**) |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | **動的決済** (DTSにより利益最大化) |\n" 
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str} / **初期追跡ストップ**) |\n"
        f"----------------------------------\n"
    )

    # 統合分析サマリー
    analysis_detail = (
        f"**🧪 統合分析サマリー (確信度を高めた要因)**\n"
        f"----------------------------------\n"
        f"| MACDh: **{tech_data.get('macd_h', 0.0):.4f}** (加速) | RSI: **{tech_data.get('rsi', 0.0):.2f}** (過熱/転換) |\n"
        f"| ADX: **{tech_data.get('adx', 0.0):.2f}** (トレンド強度) | FR Bias: **{tech_data.get('fr', 0.0)*100:.4f}%** (需給バイアス) |\n"
        f"| 4H Trend: **{long_term_trend}** | EAS: **{best_signal.get('entry_advantage_score', 0.0):.2f}** (優位性) |\n"
        f"----------------------------------\n"
    )

    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {tech_data.get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | **v16.0.0** - Adaptive EAS & Dynamic Risk |\n" 
        f"==================================\n"
        f"\n<pre>※ Market Entryは即時約定、Limit Entryは指値で約定を待ちます。DTSでは、約定後、{side}方向に価格が動いた場合、SLが自動的に追跡され利益を最大化します。初期の追跡幅はATRの{dynamic_atr_multiplier:.1f}倍です。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v16.0.0 - Adaptive EAS & Dynamic Risk")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v16.0.0 Startup initializing...") 
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
        "bot_version": "v16.0.0 - Adaptive EAS & Dynamic Risk",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v16.0.0)"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
