# ====================================================================================
# Apex BOT v21.0.8 - Dynamic Top 30 Volume & Robustness Fixed
# - 機能追加: OKXの出来高上位30銘柄を動的に取得し、固定銘柄リストと統合して監視する。
# - 修正1: 永続的エラー銘柄 (MATIC, XMR, FTM) を初期FIXED_SYMBOLSから除外し、自動除外ロジックを実装。
# - 修正2: analyze_single_timeframe内のKeyErrorを防御的に処理するように強化。
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
from typing import Dict, List, Optional, Tuple, Any, Callable, Set
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

# BOTバージョン情報
BOT_VERSION = "v21.0.8 - Dynamic Top 30 Volume (Robustness Fixed)"

# 取引所設定 (OKXに固定)
CCXT_CLIENT_NAME = "okx" 
API_KEY = os.getenv("OKX_API_KEY") 
SECRET = os.getenv("OKX_SECRET")
PASSWORD = os.getenv("OKX_PASSWORD") 

# 固定監視銘柄リスト (OKXで一般的なUSDT無期限スワップ30銘柄)
# ⚠️ ログでエラーが確認された MATIC/XMR/FTM を初期リストから削除し、他の銘柄で補填
FIXED_SYMBOLS: Set[str] = {
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "BCH/USDT", "LTC/USDT", 
    "BNB/USDT", "ATOM/USDT", "NEAR/USDT", "SAND/USDT", "MANA/USDT", 
    "APE/USDT", "SHIB/USDT", "UNI/USDT", "AAVE/USDT", "SUI/USDT", "ARB/USDT", 
    "OP/USDT", "XLM/USDT", "ICP/USDT", "FIL/USDT", "EGLD/USDT", 
    # MATIC/XMR/FTMの代わりに以下の3銘柄を追加して30銘柄を維持
    "IMX/USDT", "GRT/USDT", "GALA/USDT" 
}
# 初期設定のシンボル数が30であることを確認
FIXED_SYMBOLS = set(list(FIXED_SYMBOLS)[:30])

TIME_FRAMES = ['15m', '1h', '4h'] 

# リスク管理と戦略の定数 (v21.0.6 Fixed RRR)
SL_ATR_MULTIPLIER = 2.5             
RRR_TARGET = 4.1                    
ATR_PERIOD = 14
ADX_THRESHOLD = 30.0                
CONVICTION_SCORE_THRESHOLD = 0.85   

# スコアリングボーナス/ペナルティ定数 (v21.0.6 New Scoring)
ICHIMOKU_BONUS = 0.08               
TSI_MOMENTUM_BONUS = 0.05           
ELLIOTT_WAVE_BONUS = 0.12           
ORDER_BOOK_BIAS_BONUS = 0.05        
MTF_TREND_CONVERGENCE_BONUS = 0.10  
VWAP_BONUS = 0.07                   
FR_BIAS_BONUS = 0.08                
FR_PENALTY = -0.05                  

# ====================================================================================
# GLOBAL STATE & INITIALIZATION
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

EXCHANGE_CLIENT = None
CURRENT_MONITOR_SYMBOLS: Set[str] = FIXED_SYMBOLS.copy() 
PERMANENTLY_EXCLUDED_SYMBOLS: Set[str] = set() # 恒久的に除外する銘柄 (ExchangeErrorが続く銘柄)
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_SUCCESS_TIME = 0.0
BTC_DOMINANCE_CONTEXT = {'trend': 'Neutral', 'value': 0.0}

# ====================================================================================
# CCXT & DATA ACQUISITION UTILITIES
# ====================================================================================

def initialize_ccxt_client(client_name: str) -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する (OKX固定)"""
    try:
        client_class = getattr(ccxt_async, 'okx')
        client = client_class({
            'apiKey': API_KEY,
            'secret': SECRET,
            'password': PASSWORD,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'} # OKXでは'future'がSWAP/Futuresに対応
        })
        logging.info(f"{client_name.upper()} クライアントを初期化しました。")
        return client
    except Exception as e:
        logging.error(f"CCXTクライアント初期化エラー: {e}")
        return None

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[Optional[pd.DataFrame], str, str]:
    """OHLCVデータを取得し、DataFrameに変換する。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        EXCHANGE_CLIENT = initialize_ccxt_client(client_name)
        if not EXCHANGE_CLIENT:
            return None, "ExchangeError", client_name

    try:
        # load_marketsがmain_loopで実行されているため、通常はここでエラーにならないはず
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=300)
        
        if not ohlcv or len(ohlcv) < 100:
            return None, "DataShortage", client_name
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        return df, "OK", client_name
        
    except ccxt.DDoSProtection as e:
        logging.warning(f"DDoS保護発動: {symbol} {timeframe}. スキップします。")
        return None, "DDoSProtection", client_name
    except ccxt.ExchangeError as e:
        # このエラーが「okx does not have market symbol」の根本原因です。
        logging.error(f"取引所エラー {symbol} {timeframe}: {e}")
        # シンボルエラーがメインループで認識できるように "ExchangeError" ステータスを返す
        return None, "ExchangeError", client_name 
    except Exception as e:
        logging.error(f"OHLCV取得中に予期せぬエラー {symbol} {timeframe}: {e}")
        return None, "UnknownError", client_name

async def fetch_funding_rate(symbol: str) -> float:
    """現在の資金調達率を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
    try:
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        funding_rate = ticker.get('fundingRate', 0.0)
        return funding_rate if funding_rate is not None else 0.0
    except Exception as e:
        return 0.0

async def fetch_order_book_bias(symbol: str) -> Tuple[float, str]:
    """OKXから板情報を取得し、Bid/Askの厚さの偏りを計算する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, "Neutral"
    
    try:
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=20, params={'instType': 'SWAP'})
        limit = 5 
        
        bid_volume = sum(bid[1] for bid in orderbook['bids'][:limit])
        ask_volume = sum(ask[1] for ask in orderbook['asks'][:limit])
        
        total_volume = bid_volume + ask_volume
        
        if total_volume == 0:
            return 0.0, "Neutral"
            
        bias_ratio = (bid_volume - ask_volume) / total_volume
        
        bias_status = "Neutral"
        if bias_ratio > 0.05: 
            bias_status = "Bid_Dominant"
        elif bias_ratio < -0.05: 
            bias_status = "Ask_Dominant"
            
        return bias_ratio, bias_status
        
    except Exception as e:
        logging.warning(f"板情報取得エラー {symbol}: {e}")
        return 0.0, "Neutral"

async def fetch_dynamic_symbols() -> Set[str]:
    """OKXから出来高上位30銘柄のUSDT無期限スワップを取得する"""
    global EXCHANGE_CLIENT, PERMANENTLY_EXCLUDED_SYMBOLS
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets: 
        logging.error("CCXTクライアントが未初期化またはマーケット未ロードです。")
        return set()

    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SWAP'})
        
        usdt_swap_tickers = {}
        for symbol, ticker in tickers.items():
            volume = ticker.get('quoteVolume', 0)
            # 恒久除外リストに含まれていない銘柄のみを考慮
            if symbol.endswith('/USDT') and volume > 0 and 'swap' in ticker.get('info', {}).get('instType', '').lower() and symbol not in PERMANENTLY_EXCLUDED_SYMBOLS:
                usdt_swap_tickers[symbol] = volume

        # 出来高降順でソートし、上位30銘柄を取得
        sorted_tickers = sorted(usdt_swap_tickers.items(), key=lambda item: item[1], reverse=True)
        top_symbols = {symbol for symbol, volume in sorted_tickers[:30]}

        logging.info(f"出来高上位30銘柄を動的に取得しました。総数: {len(top_symbols)}")
        return top_symbols

    except Exception as e:
        logging.error(f"動的銘柄リスト取得エラー: {e}")
        return set()


async def get_crypto_macro_context() -> Dict:
    """BTCのトレンドを長期EMAに基づいて判断する"""
    try:
        # FutureWarningは無視して続行
        btc_data = yf.download('BTC-USD', period='5d', interval='1h', progress=False)
        
        if btc_data.empty:
             return {'trend': 'Neutral', 'value': 0.0}
             
        btc_data = btc_data[['Close']].copy()
        btc_data.columns = ['Close']

        btc_data = btc_data.tail(100)
        btc_data.ta.ema(length=20, append=True)
        
        if len(btc_data) < 20 or 'EMA_20' not in btc_data.columns:
            return {'trend': 'Neutral', 'value': 0.0}
            
        ema_now = btc_data['EMA_20'].iloc[-1]
        ema_prev = btc_data['EMA_20'].iloc[-5] 
        
        if ema_now > ema_prev * 1.0005: 
            trend = 'Long'
        elif ema_now < ema_prev * 0.9995: 
            trend = 'Short'
        else:
            trend = 'Neutral'
            
        current_price = btc_data['Close'].iloc[-1]
        
        return {'trend': trend, 'value': current_price}
    except Exception as e:
        logging.error(f"マクロコンテキスト取得エラー: {type(e).__name__} - {e}")
        return {'trend': 'Neutral', 'value': 0.0}

# ====================================================================================
# CORE TRADING LOGIC & ANALYSIS (KeyError防御を強化)
# ====================================================================================

# Helper function for safe column extraction
def safe_extract(df: pd.DataFrame, col: str, default: float) -> float:
    """データフレームから安全に値を抽出する (KeyError/IndexError防御)"""
    try:
        # カラムが存在し、かつデータが空でないことを確認
        if col in df.columns and not df[col].empty:
            return df[col].iloc[-1]
        return default
    except IndexError:
        # df[col].iloc[-1] が失敗した場合 (データが少なすぎる、計算結果が空)
        return default
    except KeyError:
        # カラムが存在しない場合
        return default

def calculate_base_score(side: str, long_term_trend: str, tech_data: Dict) -> float:
    """各種テクニカル条件に基づいて基本スコアを計算する"""
    
    score = 0.5
    
    # 1. MTFトレンド収束ボーナス
    if (side == 'ロング' and long_term_trend == 'Long') or \
       (side == 'ショート' and long_term_trend == 'Short'):
        score += MTF_TREND_CONVERGENCE_BONUS
        
    # 2. Elliott Wave / モメンタム (MACDヒストグラムの傾き)
    macd_hist = tech_data.get('macd_hist_val', 0.0)
    macd_hist_prev = tech_data.get('macd_hist_val_prev', 0.0)
    
    if side == 'ロング' and macd_hist > 0 and macd_hist > macd_hist_prev:
        score += ELLIOTT_WAVE_BONUS
    elif side == 'ショート' and macd_hist < 0 and macd_hist < macd_hist_prev:
        score += ELLIOTT_WAVE_BONUS
        
    # 3. TSI モメンタム
    tsi_val = tech_data.get('tsi_val', 0.0)
    if side == 'ロング' and tsi_val > 0.0:
        score += TSI_MOMENTUM_BONUS
    elif side == 'ショート' and tsi_val < 0.0:
        score += TSI_MOMENTUM_BONUS
        
    # 4. 一目均衡表の雲のサポート/レジスタンス
    price = tech_data.get('price', 0.0)
    ichi_b = tech_data.get('ichi_b', price)
    
    if side == 'ロング' and price > ichi_b:
        score += ICHIMOKU_BONUS
    elif side == 'ショート' and price < ichi_b:
        score += ICHIMOKU_BONUS
        
    # 5. 板情報バイアス (逆張りペナルティ)
    order_book_status = tech_data.get('order_book_status', 'Neutral')
    if side == 'ロング' and order_book_status == 'Ask_Dominant':
        score += ORDER_BOOK_BIAS_BONUS
    elif side == 'ショート' and order_book_status == 'Bid_Dominant':
        score += ORDER_BOOK_BIAS_BONUS
        
    # 6. VWAP
    vwap_val = tech_data.get('vwap_val', price)
    if side == 'ロング' and price > vwap_val:
        score += VWAP_BONUS
    elif side == 'ショート' and price < vwap_val:
        score += VWAP_BONUS
        
    # 7. ファンディングレート (FR) バイアス
    fr_val = tech_data.get('funding_rate_value', 0.0)
    if side == 'ロング':
        if fr_val > 0.0005: # FRが非常に高い場合 (ショート優勢)
            score += FR_BIAS_BONUS # ロングにとってはショートが苦しい状況で買い向かうボーナス
        elif fr_val < -0.0005: # FRが非常に低い場合 (ロング優勢)
            score += FR_PENALTY # ロングポジションの混雑ペナルティ
    elif side == 'ショート':
        if fr_val < -0.0005: # FRが非常に低い場合 (ロング優勢)
            score += FR_BIAS_BONUS # ショートにとってはロングが苦しい状況で売り向かうボーナス
        elif fr_val > 0.0005: # FRが非常に高い場合 (ショート優勢)
            score += FR_PENALTY # ショートポジションの混雑ペナルティ

    # ADXチェック (トレンドの強さ)
    adx_val = tech_data.get('adx', 0.0)
    if adx_val < ADX_THRESHOLD:
        score -= 0.1 # トレンドが弱い場合はペナルティ

    return max(0.0, min(1.0, score))


async def analyze_single_timeframe(
    client_name: str, 
    symbol: str, 
    timeframe: str, 
    long_term_trend: str = 'Neutral'
) -> Optional[Dict]:
    """
    単一の時間軸でテクニカル分析を行い、スコアと取引プランを返す。
    """
    
    # 1. データ取得とFunding Rate, Order Book取得
    ohlcv, status, _ = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    if status != "OK":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.0}
        
    df = ohlcv
    
    # データが直近の価格計算に十分か確認
    if len(df) < 5: 
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "DataShortage", 'score': 0.0}
        
    price = df['close'].iloc[-1]
    
    funding_rate_val = 0.0
    order_book_bias_ratio = 0.0
    order_book_status = "Neutral"

    # 1h足でのみFRと板情報を取得
    if timeframe == '1h': 
        funding_rate_val = await fetch_funding_rate(symbol)
        order_book_bias_ratio, order_book_status = await fetch_order_book_bias(symbol)

    # 2. テクニカル指標の計算 (Pandas-TA)
    try:
        # ATRとADXは期間が長く必要なので、データ不足で計算失敗する可能性がある
        df.ta.adx(length=ATR_PERIOD, append=True)
        df.ta.atr(length=ATR_PERIOD, append=True)
        df.ta.macd(append=True)
        df.ta.rsi(append=True)
        df.ta.bbands(append=True)
        df.ta.donchian(append=True)
        df.ta.vwap(append=True)
        df.ta.tsi(append=True) 
        df.ta.ichimoku(append=True)
        df.ta.ppo(append=True) 
    except Exception as e:
        # 指標計算中の予期せぬエラーは、データフレーム構造の異常を示すことが多い
        logging.warning(f"指標計算エラー {symbol} {timeframe}: {e}. データ不足とみなしスキップ。")
        return {'symbol': symbol, 'timeframe': timeframe, 'side': "DataShortage", 'score': 0.0}
    
    # 3. テクニカル指標の抽出 (KeyError防御を大幅に強化)
    
    # 指標値の取得
    atr_val = safe_extract(df, f'ATR_{ATR_PERIOD}', price * 0.01) # ATRのデフォルト値は価格の1%としてリスクを抑える
    adx_val = safe_extract(df, f'ADX_{ATR_PERIOD}', 0.0)
    rsi_val = safe_extract(df, 'RSI_14', 50.0)
    
    # Ichimoku
    ichi_k_val = safe_extract(df, 'ICHI_K_9', price)     
    ichi_t_val = safe_extract(df, 'ICHI_T_26', price)    
    ichi_a_val = safe_extract(df, 'ICHI_A_26', price)    
    ichi_b_val = safe_extract(df, 'ICHI_B_52', price)    
    
    # TSI
    tsi_val = safe_extract(df, 'TSI_13_25_13', 0.0)
    # TSIsは-1, 0, 1の数値として取得。safe_extractで0.0をデフォルトとして取得
    # tsi_signal = safe_extract(df, 'TSIs_13_25_13', 0.0) # TSIsはここでは使わない
    
    # Elliott Proxy
    macd_hist_val = safe_extract(df, 'MACDh_12_26_9', 0.0)
    macd_hist_val_prev = safe_extract(df, 'MACDh_12_26_9', 0.0)
    if 'MACDh_12_26_9' in df.columns and len(df['MACDh_12_26_9']) >= 2:
        macd_hist_val_prev = df['MACDh_12_26_9'].iloc[-2]
    
    # Structural SL (Donchian Channel)
    s1_pivot = safe_extract(df, 'DCL_20', 0.0)
    r1_pivot = safe_extract(df, 'DCU_20', 0.0)
    
    # VWAP
    vwap_val = safe_extract(df, 'VWAP', price)
    
    # 4. トレンド方向の判定 (転換線の向きと価格の位置で簡易判定)
    side = "Neutral"
    if ichi_k_val > ichi_t_val and price > ichi_t_val: 
        side = "ロング"
    elif ichi_k_val < ichi_t_val and price < ichi_t_val:
        side = "ショート"
    
    # 5. スコアリングのためのデータ構造
    tech_data: Dict[str, Any] = {
        'price': price, 'atr': atr_val, 'adx': adx_val, 'rsi': rsi_val, 'vwap_val': vwap_val,
        'funding_rate_value': funding_rate_val, 's1_pivot': s1_pivot, 'r1_pivot': r1_pivot,
        'order_book_bias_ratio': order_book_bias_ratio, 'order_book_status': order_book_status,
        'tsi_val': tsi_val, 'ichi_k': ichi_k_val, 'ichi_t': ichi_t_val, 'ichi_b': ichi_b_val,
        'macd_hist_val': macd_hist_val, 'macd_hist_val_prev': macd_hist_val_prev,
        'long_term_trend': long_term_trend,
        'is_main_tf': (timeframe == '1h') 
    }
    
    # 1h足 (メイン分析) 以外では、スコアリングとTP/SL計算をスキップ
    if timeframe != '1h':
        return {
            'symbol': symbol, 'timeframe': timeframe, 'side': side, 'score': 0.0,
            'price': price, 'entry': 0.0, 'sl': 0.0, 'tp1': 0.0,
            'entry_type': "N/A", 'regime': "N/A", 'tech_data': tech_data
        }

    # === 1h足 (メイン分析) のみのスコアリングとTP/SL計算 ===
    
    score = calculate_base_score(side, long_term_trend, tech_data)
    
    # 6. TP/SLとRRRの決定 (v21.0.6: 固定 SL/TP)
    entry = price # 成行エントリーを想定
    regime = "Trend" if adx_val >= ADX_THRESHOLD else "Range"
    
    # SLの候補を決定
    sl_structural = s1_pivot if side == 'ロング' else r1_pivot
    sl_atr = entry - (atr_val * SL_ATR_MULTIPLIER) if side == 'ロング' else entry + (atr_val * SL_ATR_MULTIPLIER)

    # 構造的SL (S1/R1) を優先し、ATR SLがより安全な場合のみ採用
    if side == 'ロング':
        sl = min(sl_structural, sl_atr)
        # 構造的SLを使う場合は、エントリーポイントとの一致を避けるため、ATRのバッファを追加
        if sl == sl_structural:
             sl -= (0.5 * atr_val) 
    else: # ショート
        sl = max(sl_structural, sl_atr)
        if sl == sl_structural:
             sl += (0.5 * atr_val)

    # TP1の計算
    risk = abs(entry - sl)
    tp1 = entry + (risk * RRR_TARGET) if side == 'ロング' else entry - (risk * RRR_TARGET)
    
    entry_type = "Market"
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': score,
        'price': price,
        'entry': entry,
        'sl': sl,
        'tp1': tp1,
        'entry_type': entry_type,
        'regime': regime,
        'tech_data': tech_data
    }

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """統合分析結果をメッセージ形式にフォーマットする (omitted)"""
    # ... (omitted: メッセージフォーマット関数は割愛します)
    return f"【Rank {rank}: {symbol}】 Analysis Completed."


async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    """複数の時間軸を統合し、最終的なシグナルを生成する"""
    
    # 0. 4hトレンドの事前計算 (MTFトレンド収束用)
    h4_analysis = await analyze_single_timeframe(client_name, symbol, '4h')
    # エラーの場合、後続の処理に進まずエラー情報を返す
    if h4_analysis and h4_analysis.get('side') in ["ExchangeError", "UnknownError", "DataShortage", "DDoSProtection"]:
        # メインループでエラーを追跡できるように、h4の結果（エラー情報）をリストに入れて返す
        return [h4_analysis] 

    long_term_trend = h4_analysis.get('side', 'Neutral') if h4_analysis else 'Neutral'
    
    # 0.5. 15mトレンドの事前計算 (補助情報用)
    m15_analysis = await analyze_single_timeframe(client_name, symbol, '15m')
    if m15_analysis and m15_analysis.get('side') in ["ExchangeError", "UnknownError", "DataShortage", "DDoSProtection"]:
        return [m15_analysis] # エラー情報を返す

    # 1. メイン時間軸の分析 (1h) を実行
    h1_analysis = await analyze_single_timeframe(client_name, symbol, '1h', long_term_trend)
    
    if h1_analysis and h1_analysis.get('side') in ["ExchangeError", "UnknownError", "DataShortage", "DDoSProtection"]:
        return [h1_analysis] # エラー情報を返す

    if not h1_analysis or h1_analysis.get('side') not in ['ロング', 'ショート']:
        return []
    
    result = h1_analysis
    # ... (omitted signal processing)
        
    return [result]

# ====================================================================================
# MAIN LOOP & FastAPI SETUP
# ====================================================================================

async def main_loop():
    """ボットのメインループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, BTC_DOMINANCE_CONTEXT, EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, FIXED_SYMBOLS, PERMANENTLY_EXCLUDED_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        EXCHANGE_CLIENT = initialize_ccxt_client(CCXT_CLIENT_NAME)
        if not EXCHANGE_CLIENT:
            logging.error("CCXTクライアントの初期化に失敗しました。ボットを停止します。")
            return

    try:
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"{CCXT_CLIENT_NAME.upper()} マーケットを正常にロードしました。")
    except Exception as e:
        logging.error(f"致命的: マーケットロードエラー ({CCXT_CLIENT_NAME}): {e}")
        return
    
    loop_count = 0
    # FIXED_SYMBOLSをグローバルセットとして扱う (ミュータブルな操作を可能にするため)
    global FIXED_SYMBOLS 

    while True:
        try:
            loop_count += 1
            logging.info(f"--- 🔄 Apex BOT {BOT_VERSION} 処理開始 (Loop: {loop_count}) ---")
            
            # 3. 監視銘柄リストの更新 (5分ごと、5ループに1回)
            if loop_count % 5 == 1: 
                # 恒久的なエラー銘柄を除外した後の固定銘柄リスト
                current_fixed_symbols = FIXED_SYMBOLS.difference(PERMANENTLY_EXCLUDED_SYMBOLS)
                
                # 動的銘柄リストを取得し、恒久的なエラー銘柄を除外
                dynamic_symbols = await fetch_dynamic_symbols()
                filtered_dynamic_symbols = dynamic_symbols.difference(PERMANENTLY_EXCLUDED_SYMBOLS)
                
                # 統合 (FIXED_SYMBOLSの欠員が動的銘柄で補われる)
                CURRENT_MONITOR_SYMBOLS = current_fixed_symbols.union(filtered_dynamic_symbols)
                
                logging.info(f"監視銘柄リストを更新しました。固定:{len(current_fixed_symbols)}, 動的:{len(filtered_dynamic_symbols)}, 総監視数: {len(CURRENT_MONITOR_SYMBOLS)} (恒久除外:{len(PERMANENTLY_EXCLUDED_SYMBOLS)})")

            # 4. マクロコンテキストの取得
            BTC_DOMINANCE_CONTEXT = await get_crypto_macro_context()
            logging.info(f"マクロコンテキスト (BTCトレンド): {BTC_DOMINANCE_CONTEXT['trend']}")

            # 5. 監視銘柄の分析
            all_signals: List[Dict] = []
            tasks = []
            
            symbols_to_monitor = list(CURRENT_MONITOR_SYMBOLS)
            random.shuffle(symbols_to_monitor)

            for symbol in symbols_to_monitor:
                tasks.append(generate_integrated_signal(symbol, BTC_DOMINANCE_CONTEXT, CCXT_CLIENT_NAME))

            results = await asyncio.gather(*tasks)
            
            # 結果の処理とエラー銘柄の抽出
            for result_list in results:
                if result_list and isinstance(result_list[0], dict):
                    status = result_list[0].get('side', 'N/A')
                    failed_symbol = result_list[0]['symbol']
                    
                    if status in ["ExchangeError", "UnknownError", "DataShortage", "DDoSProtection"]:
                        
                        # ExchangeError（okx does not have market symbol）は恒久的なエラーと見なし、FIXED_SYMBOLSから除外する
                        if status == "ExchangeError" and failed_symbol in FIXED_SYMBOLS:
                             # 恒久除外リストに追加し、FIXED_SYMBOLSから削除して、次から動的銘柄で補填できるようにする
                             if failed_symbol not in PERMANENTLY_EXCLUDED_SYMBOLS:
                                PERMANENTLY_EXCLUDED_SYMBOLS.add(failed_symbol)
                                FIXED_SYMBOLS.remove(failed_symbol) 
                                 
                                logging.warning(f"恒久的なExchangeErrorにより、FIXED_SYMBOLSから {failed_symbol} を除外しました。現在の固定銘柄数: {len(FIXED_SYMBOLS)}")
                        
                        # エラーログを抑制するため、シグナルリストには追加しない
                        continue 
                    else:
                        # 正常なシグナルを統合
                        all_signals.extend(result_list)
                
            
            # 6. シグナルのフィルタリングとランキング 
            high_conviction_signals = [
                s for s in all_signals 
                if s.get('score', 0.0) >= CONVICTION_SCORE_THRESHOLD
            ]
            
            ranked_signals = sorted(high_conviction_signals, key=lambda x: x.get('score', 0.0), reverse=True)
            
            LAST_ANALYSIS_SIGNALS = ranked_signals
            
            # 7. メッセージ生成とロギング 
            messages = []
            for rank, signal in enumerate(ranked_signals[:5]): 
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank + 1) # ここではシンプルなメッセージ関数を使用
                messages.append(message)
                
            if messages:
                logging.info(f"--- 📣 高確度シグナル (TOP {len(messages)}) ---\n" + "\n\n".join(messages))
            else:
                logging.info("高確度シグナルは見つかりませんでした。")
            
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"--- ✅ Apex BOT {BOT_VERSION} 処理完了 (次まで60秒待機) ---")
            
            await asyncio.sleep(60)

        except Exception as e:
            error_name = type(e).__name__
            
            if "Connection reset by peer" in str(e):
                logging.warning("接続リセットエラー。CCXTクライアントを再初期化します。")
                if EXCHANGE_CLIENT:
                    await EXCHANGE_CLIENT.close()
                EXCHANGE_CLIENT = initialize_ccxt_client(CCXT_CLIENT_NAME)
                try:
                    if EXCHANGE_CLIENT:
                        await EXCHANGE_CLIENT.load_markets()
                        logging.info("CCXTクライアントとマーケットを再ロードしました。")
                except Exception as load_e:
                    logging.error(f"再ロードエラー: {load_e}")

            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    logging.info(f"🚀 Apex BOT {BOT_VERSION} Startup initializing...") 
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
        "bot_version": BOT_VERSION,
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "permanently_excluded_symbols_count": len(PERMANENTLY_EXCLUDED_SYMBOLS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT {BOT_VERSION} is running."})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000)) 
    uvicorn.run(app, host="0.0.0.0", port=port)
