# ====================================================================================
# Apex BOT v21.0.6 - Elliott/TSI/Ichimoku/OrderBook/FixedRRR Strategy (マーケットロードFIX)
# - 修正1: OKXシンボルエラー (okx does not have market symbol) に対処するため、
#          main_loopの開始時にマーケットリストを非同期でロードする処理を追加。
# - 修正2: yfinance (BTCマクロ) のインデックスエラーに対処するため、データ処理を堅牢化。
# - 機能: 固定30銘柄 + OKXの出来高上位30銘柄を動的に監視。
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
BOT_VERSION = "v21.0.6 - Dynamic Top 30 Volume (Market Load Fixed)"

# 取引所設定 (OKXに固定)
CCXT_CLIENT_NAME = "okx" 
API_KEY = os.getenv("OKX_API_KEY") 
SECRET = os.getenv("OKX_SECRET")
PASSWORD = os.getenv("OKX_PASSWORD") 

# 固定監視銘柄リスト (OKXで一般的なUSDT無期限スワップ30銘柄)
FIXED_SYMBOLS: Set[str] = {
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "MATIC/USDT", "LINK/USDT", "BCH/USDT", "LTC/USDT", 
    "BNB/USDT", "ATOM/USDT", "NEAR/USDT", "FTM/USDT", "SAND/USDT", "MANA/USDT", 
    "APE/USDT", "SHIB/USDT", "UNI/USDT", "AAVE/USDT", "SUI/USDT", "ARB/USDT", 
    "OP/USDT", "XLM/USDT", "ICP/USDT", "FIL/USDT", "EGLD/USDT", "XMR/USDT"
}
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
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_SUCCESS_TIME = 0.0
BTC_DOMINANCE_CONTEXT = {'trend': 'Neutral', 'value': 0.0}

# ====================================================================================
# CCXT & DATA ACQUISITION UTILITIES
# ====================================================================================

def initialize_ccxt_client(client_name: str) -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する (OKX固定) - マーケットロードはmain_loopで行う"""
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
        # このパスはmain_loopが起動している限り基本的に通らない
        EXCHANGE_CLIENT = initialize_ccxt_client(client_name)
        if not EXCHANGE_CLIENT:
            return None, "ExchangeError", client_name

    try:
        # await client.load_markets() が main_loopで実行されている前提
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
        # マーケットロードの失敗を示すことが多いため、エラーログレベルを維持
        logging.error(f"取引所エラー {symbol} {timeframe}: {e}")
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
        # OKXの無期限スワップを指定
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
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets: # マーケットがロードされていることを確認
        logging.error("CCXTクライアントが未初期化またはマーケット未ロードです。")
        return set()

    try:
        # すべてのティッカーを取得
        # OKXの場合、instType='SWAP'を指定することで無期限スワップのみに絞り込む
        tickers = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SWAP'})
        
        # フィルタリングとソーティング
        usdt_swap_tickers = {}
        for symbol, ticker in tickers.items():
            # CCXTの標準シンボル形式で、かつ24h出来高情報があるもの
            volume = ticker.get('quoteVolume', 0)
            if symbol.endswith('/USDT') and volume > 0 and 'swap' in ticker.get('info', {}).get('instType', '').lower():
                usdt_swap_tickers[symbol] = volume

        # 出来高降順でソートし、上位30銘柄を取得
        sorted_tickers = sorted(usdt_swap_tickers.items(), key=lambda item: item[1], reverse=True)
        # 上位30銘柄のシンボルのみをセットとして抽出
        top_symbols = {symbol for symbol, volume in sorted_tickers[:30]}

        logging.info(f"出来高上位30銘柄を動的に取得しました。総数: {len(top_symbols)}")
        return top_symbols

    except Exception as e:
        logging.error(f"動的銘柄リスト取得エラー: {e}")
        return set()


async def get_crypto_macro_context() -> Dict:
    """BTCドミナンスの動向を取得する"""
    try:
        # yfinanceのFutureWarningを無視し、データ取得
        btc_data = yf.download('BTC-USD', period='5d', interval='1h', progress=False)
        
        # --- 修正: DataFrameの構造をシンプルにする (MultiIndexエラー対策) ---
        if btc_data.empty:
             return {'trend': 'Neutral', 'value': 0.0}
             
        # 'Close'列のみを抽出し、コピーを作成（念のため）
        btc_data = btc_data[['Close']].copy()
        btc_data.columns = ['Close']
        # ------------------------------------------------------------------

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
        # インデックスエラー、ネットワークエラーなどに備える
        logging.error(f"マクロコンテキスト取得エラー: {type(e).__name__} - {e}")
        return {'trend': 'Neutral', 'value': 0.0}

# ====================================================================================
# CORE TRADING LOGIC & ANALYSIS (v21.0.6)
# ====================================================================================

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
    price = df['close'].iloc[-1]
    
    funding_rate_val = 0.0
    order_book_bias_ratio = 0.0
    order_book_status = "Neutral"

    # 1h足でのみFRと板情報を取得
    if timeframe == '1h': 
        funding_rate_val = await fetch_funding_rate(symbol)
        order_book_bias_ratio, order_book_status = await fetch_order_book_bias(symbol)

    # 2. テクニカル指標の計算 (Pandas-TA)
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
    
    # 3. テクニカル指標の抽出
    atr_val = df[f'ATR_{ATR_PERIOD}'].iloc[-1]
    adx_val = df[f'ADX_{ATR_PERIOD}'].iloc[-1]
    rsi_val = df['RSI_14'].iloc[-1]
    
    # Ichimoku
    ichi_k_val = df['ICHI_K_9'].iloc[-1]     
    ichi_t_val = df['ICHI_T_26'].iloc[-1]    
    ichi_a_val = df['ICHI_A_26'].iloc[-1]    
    ichi_b_val = df['ICHI_B_52'].iloc[-1]    
    
    # TSI
    tsi_val = df['TSI_13_25_13'].iloc[-1]
    tsi_signal = df['TSIs_13_25_13'].iloc[-1] 
    
    # Elliott Proxy
    macd_hist_val = df['MACDh_12_26_9'].iloc[-1]
    macd_hist_val_prev = df['MACDh_12_26_9'].iloc[-2]
    ppo_hist_val = df['PPOh_12_26_9'].iloc[-1]
    
    # Structural SL
    dc_cols_present = 'DCU_20' in df.columns and 'DCL_20' in df.columns
    s1_pivot = df['DCL_20'].iloc[-2] if dc_cols_present else 0.0 
    r1_pivot = df['DCU_20'].iloc[-2] if dc_cols_present else 0.0 
    
    # VWAP
    vwap_val = df['VWAP'].iloc[-1] if 'VWAP' in df.columns else price
    
    # 4. トレンド方向の判定 (基本トレンドとスコアリングベース)
    side = "Neutral"
    if ichi_k_val > ichi_t_val and price > ichi_t_val: 
        side = "ロング"
    elif ichi_k_val < ichi_t_val and price < ichi_t_val:
        side = "ショート"
    
    # 5. スコアリング (ベース 0.5 + ボーナス/ペナルティ)
    score = 0.5
    tech_data: Dict[str, Any] = {
        'atr': atr_val, 'adx': adx_val, 'rsi': rsi_val, 'vwap_val': vwap_val,
        'funding_rate_value': funding_rate_val, 's1_pivot': s1_pivot, 'r1_pivot': r1_pivot,
        'order_book_bias_ratio': order_book_bias_ratio, 'order_book_status': order_book_status,
        'tsi_val': tsi_val, 'ichi_k': ichi_k_val, 'ichi_t': ichi_t_val, 
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
    
    regime = "レンジ/弱いトレンド"
    if adx_val >= ADX_THRESHOLD:
        regime = "強いトレンド"
        score += 0.1 
        
    if (side == "ロング" and rsi_val < 65) or (side == "ショート" and rsi_val > 35):
        score += 0.05
    
    fr_bonus = 0.0
    if side == "ロング" and funding_rate_val < -0.0005: 
        fr_bonus = FR_BIAS_BONUS
    elif side == "ショート" and funding_rate_val > 0.0005: 
        fr_bonus = FR_BIAS_BONUS
    elif side == "ロング" and funding_rate_val > 0.001: 
        fr_bonus = FR_PENALTY
    elif side == "ショート" and funding_rate_val < -0.001: 
        fr_bonus = FR_PENALTY
    score = min(1.0, score + fr_bonus)
    tech_data['funding_rate_bonus_value'] = fr_bonus
    
    vwap_bonus = 0.0
    if abs(price - vwap_val) / price < 0.001: 
        vwap_bonus = VWAP_BONUS
    score = min(1.0, score + vwap_bonus)
    tech_data['vwap_bonus'] = vwap_bonus

    ichi_bonus = 0.0
    ichi_status = "Neutral"
    if ichi_a_val > ichi_b_val and price > max(ichi_a_val, ichi_b_val) and ichi_k_val > ichi_t_val:
        if side == "ロング":
            ichi_bonus = ICHIMOKU_BONUS
            ichi_status = "Strong_Long"
    elif ichi_a_val < ichi_b_val and price < min(ichi_a_val, ichi_b_val) and ichi_k_val < ichi_t_val:
        if side == "ショート":
            ichi_bonus = ICHIMOKU_BONUS
            ichi_status = "Strong_Short"

    score = min(1.0, score + ichi_bonus)
    tech_data['ichi_bonus'] = ichi_bonus
    tech_data['ichi_status'] = ichi_status

    tsi_bonus = 0.0
    if side == "ロング" and tsi_val > 5 and tsi_signal == 1: 
        tsi_bonus = TSI_MOMENTUM_BONUS
    elif side == "ショート" and tsi_val < -5 and tsi_signal == -1: 
        tsi_bonus = TSI_MOMENTUM_BONUS
    score = min(1.0, score + tsi_bonus)
    tech_data['tsi_bonus'] = tsi_bonus

    elliott_bonus = 0.0
    elliott_status = "修正波/Neutral"
    if side == "ロング" and macd_hist_val > macd_hist_val_prev and ppo_hist_val > 0.0:
        elliott_bonus = ELLIOTT_WAVE_BONUS
        elliott_status = "推進波継続 (Wave 3/5)"
    elif side == "ショート" and macd_hist_val < macd_hist_val_prev and ppo_hist_val < 0.0:
        elliott_bonus = ELLIOTT_WAVE_BONUS
        elliott_status = "推進波継続 (Wave 3/5)"

    score = min(1.0, score + elliott_bonus)
    tech_data['elliott_bonus'] = elliott_bonus
    tech_data['elliott_status'] = elliott_status

    order_book_bonus = 0.0
    if order_book_status == "Bid_Dominant" and side == "ロング":
        order_book_bonus = ORDER_BOOK_BIAS_BONUS
    elif order_book_status == "Ask_Dominant" and side == "ショート":
        order_book_bonus = ORDER_BOOK_BIAS_BONUS
    score = min(1.0, score + order_book_bonus)
    tech_data['order_book_bonus'] = order_book_bonus
    
    # 6. TP/SLとRRRの決定 (v21.0.6: 固定 SL/TP)
    rr_base = RRR_TARGET
    entry = price 
    sl = 0
    tp1 = 0
    structural_sl_pivot = s1_pivot if side == "ロング" else r1_pivot
    
    use_limit_entry = score < 0.70 
    entry_type = "Limit" if use_limit_entry else "Market"
    sl_dist_atr = atr_val * SL_ATR_MULTIPLIER 
    
    if side == "ロング":
        if use_limit_entry:
            bb_low = df['BBL_20_2.0'].iloc[-1] if 'BBL_20_2.0' in df.columns else price
            entry = min(bb_low, price) 
        else:
            entry = price
        
        sl = entry - sl_dist_atr 
        
        if structural_sl_pivot > 0 and structural_sl_pivot > sl and structural_sl_pivot < entry:
             sl = structural_sl_pivot - atr_val * 0.5 
             
        if sl <= 0: sl = entry * 0.99 
        
        tp_dist = abs(entry - sl) * rr_base 
        tp1 = entry + tp_dist
        
        tech_data['psychological_sl'] = s1_pivot if s1_pivot > 0 and s1_pivot < entry else sl

        
    elif side == "ショート":
        if use_limit_entry:
            bb_high = df['BBU_20_2.0'].iloc[-1] if 'BBU_20_2.0' in df.columns else price
            entry = max(bb_high, price) 
        else:
            entry = price
        
        sl = entry + sl_dist_atr
        
        if structural_sl_pivot > 0 and structural_sl_pivot < sl and structural_sl_pivot > entry:
             sl = structural_sl_pivot + atr_val * 0.5 
             
        tp_dist = abs(entry - sl) * rr_base 
        tp1 = entry - tp_dist
        
        tech_data['psychological_sl'] = r1_pivot if r1_pivot > 0 and r1_pivot > entry else sl


    # 7. 結果の統合
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

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    """複数の時間軸を統合し、最終的なシグナルを生成する"""
    
    # 0. 4hトレンドの事前計算 (MTFトレンド収束用)
    h4_analysis = await analyze_single_timeframe(client_name, symbol, '4h')
    long_term_trend = h4_analysis.get('side', 'Neutral') if h4_analysis else 'Neutral'
    
    # 0.5. 15mトレンドの事前計算 (補助情報用)
    m15_analysis = await analyze_single_timeframe(client_name, symbol, '15m')
    short_term_trend = m15_analysis.get('side', 'Neutral') if m15_analysis else 'Neutral'

    # 1. メイン時間軸の分析 (1h) を実行
    h1_analysis = await analyze_single_timeframe(client_name, symbol, '1h', long_term_trend)
    
    if not h1_analysis or h1_analysis.get('side') not in ['ロング', 'ショート']:
        return []
    
    # 2. MTF スコアリングブーストロジック & トレンド収束ボーナス (1h足に対して適用)
    result = h1_analysis
    side = result['side']
    score = result['score']
    
    # BTCドミナンスバイアスを適用 
    dominance_bias_bonus_value = 0.0
    if symbol != "BTC/USDT":
        if (side == 'ロング' and macro_context['trend'] == 'Short') or \
           (side == 'ショート' and macro_context['trend'] == 'Long'):
            dominance_bias_bonus_value = 0.0 
        else:
            dominance_bias_bonus_value = 0.02 
    
    result['score'] = min(1.0, score + dominance_bias_bonus_value)
    result.setdefault('tech_data', {})['dominance_trend'] = macro_context['trend']
    result.setdefault('tech_data', {})['dominance_bias_bonus_value'] = dominance_bias_bonus_value

    # 4hトレンドとの収束チェック (MTF Convergence Bonus 0.10)
    mtf_convergence_bonus = 0.0
    if (side == 'ロング' and long_term_trend == 'ロング') or \
       (side == 'ショート' and long_term_trend == 'ショート'):
           mtf_convergence_bonus = MTF_TREND_CONVERGENCE_BONUS
           
    result['score'] = min(1.0, result['score'] + mtf_convergence_bonus)
    result.setdefault('tech_data', {})['mtf_convergence_bonus'] = mtf_convergence_bonus
    
    # 15mトレンドを補助情報として格納
    result.setdefault('tech_data', {})['short_term_trend'] = short_term_trend
        
    return [result]

# ====================================================================================
# OUTPUT FORMATTING
# ====================================================================================

def format_price_utility(price: float, symbol: str) -> str:
    """価格をシンボルに応じて適切な桁数でフォーマットする"""
    if "BTC" in symbol or "ETH" in symbol:
        return f"{price:,.2f}"
    if price >= 1.0:
        return f"{price:,.4f}"
    return f"{price:.6f}"

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """統合された分析結果を、v21.0.6のメッセージフォーマットに合わせて整形する"""
    
    high_score_signals = [s for s in signals if s.get('score', 0.0) >= CONVICTION_SCORE_THRESHOLD and s.get('side') in ['ロング', 'ショート']]
    
    if not high_score_signals:
        return ""
        
    best_signal = sorted(high_score_signals, key=lambda x: x.get('score', 0.0), reverse=True)[0]

    display_symbol = symbol.replace('/USDT', '')
    side = best_signal.get('side', 'N/A')
    score = best_signal.get('score', 0.0)
    score_100 = score * 100
    price = best_signal.get('price', 0.0)
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0) 
    sl_price = best_signal.get('sl', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A') 
    tech_data = best_signal.get('tech_data', {})
    
    psychological_sl = tech_data.get('psychological_sl', sl_price)

    # 3. PnL予測とRRRの計算
    sl_width = abs(entry_price - sl_price)
    reward_usd = abs(tp_price - entry_price)
    rr_actual = reward_usd / sl_width if sl_width > 0 else 0.0
    
    risk_pct = sl_width / entry_price * 100
    reward_pct = reward_usd / entry_price * 100
    
    # 4. ヘッダーとトレードプラン
    header = (
        f"🔔 **Apex BOT {BOT_VERSION.split(' ')[0]}** - 高確度シグナル #{rank} ({CCXT_CLIENT_NAME.upper()})\n"
        f"[{datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}]\n\n"
        f"👑 **{display_symbol}/{symbol.split('/')[-1]} {side} ({score_100:.1f}点)**\n"
        f"💡 **優位性**: MTFトレンド収束 & 一目均衡表の優位性\n"
    )

    trade_plan = (
        f"\n**💰 トレードプラン (RR {rr_actual:.1f}:1)**\n"
        f"----------------------------------\n"
        f"📊 **現在の価格**: `{format_price_utility(price, symbol)}`\n"
        f"🎯 **エントリー ({entry_type})**: `{format_price_utility(entry_price, symbol)}`\n"
        f"🚫 **損切り (SL)**: `{format_price_utility(sl_price, symbol)}` (心理的SL: `{format_price_utility(psychological_sl, symbol)}`)\n"
        f"✅ **利確 (TP)**: `{format_price_utility(tp_price, symbol)}`\n"
        f"\n**📝 PnL予測**\n"
        f"----------------------------------\n"
        f"└ 予想損失 (SL): -{format_price_utility(sl_width, symbol)} USD (-{risk_pct:.2f}%)\n"
        f"└ 予想利益 (TP): +{format_price_utility(reward_usd, symbol)} USD (+{reward_pct:.2f}%)\n"
    )

    # 5. 統合分析サマリー
    analysis_detail = "\n**🔬 統合分析サマリー (1h軸)**\n"
    
    # 💡 MTFトレンド収束 (4h)
    mtf_bonus = tech_data.get('mtf_convergence_bonus', 0.0)
    long_term_trend = tech_data.get('long_term_trend', 'Neutral')
    mtf_trend_str = f"4hトレンド: {long_term_trend} ({'✨' if mtf_bonus > 0 else '❌'} 収束)"
    analysis_detail += (
        f"{mtf_trend_str} (+{mtf_bonus * 100:.2f}点 ボーナス！)\n"
    )

    # 💡 短期トレンド (15m) の表示
    short_term_trend = tech_data.get('short_term_trend', 'Neutral')
    analysis_detail += f"⏱️ 15mトレンド: {short_term_trend} (短期方向)\n"

    # 💡 BTCドミナンス
    dominance_trend = tech_data.get('dominance_trend', 'Neutral')
    dominance_bonus = tech_data.get('dominance_bias_bonus_value', 0.0)
    analysis_detail += (
        f"🌏 BTCドミナンス: {dominance_trend} ({'+' if dominance_bonus >= 0 else ''}{dominance_bonus * 100:.2f}点)\n"
    )
    
    # 1h足の詳細
    regime = best_signal.get('regime', 'N/A')
    
    analysis_detail += f"[1h 足] 🔥 ({score_100:.2f}点) -> {side}\n"
    
    # エリオット波動
    elliott_status = tech_data.get('elliott_status', '修正波/Neutral')
    elliott_bonus = tech_data.get('elliott_bonus', 0.0)
    analysis_detail += f"   └ エリオット波動: {elliott_status} ({'+' if elliott_bonus >= 0 else ''}{elliott_bonus * 100:.2f}点)\n"

    # トレンド指標 (ADX/Ichimoku)
    adx_val = tech_data.get('adx', 0.0)
    ichi_status_display = "⛩️ Ichimoku OK (仮定)" if tech_data.get('ichi_bonus', 0.0) > 0 else "❌ Ichimoku NG"
    analysis_detail += f"   └ トレンド指標: ADX:{adx_val:.2f} ({regime}), {ichi_status_display}\n"
    
    # モメンタム指標 (TSI)
    tsi_val = tech_data.get('tsi_val', 0.0)
    tsi_bonus = tech_data.get('tsi_bonus', 0.0)
    tsi_display = "TSI OK" if tsi_bonus > 0 else "TSI NG"
    analysis_detail += f"   └ モメンタム指標: TSI:{tsi_val:.2f} ({tsi_display}) ({'+' if tsi_bonus >= 0 else ''}{tsi_bonus * 100:.2f}点)\n"

    # 一目均衡表の詳細
    ichi_bonus = tech_data.get('ichi_bonus', 0.0)
    ichi_detail = "雲/線優位" if ichi_bonus > 0 else "優位性なし"
    analysis_detail += f"   └ 💡 一目均衡表: {ichi_detail} ({'+' if ichi_bonus >= 0 else ''}{ichi_bonus * 100:.2f}点)\n"

    # 資金調達率 (FR)
    funding_rate_val = tech_data.get('funding_rate_value', 0.0)
    funding_rate_bonus = tech_data.get('funding_rate_bonus_value', 0.0)
    funding_rate_status = "✅ 優位性あり" if funding_rate_bonus > 0 else ("⚠️ 過密ペナルティ" if funding_rate_bonus < 0 else "❌ フィルター範囲外")
    analysis_detail += f"   └ 資金調達率 (FR): {funding_rate_val * 100:.4f}% - {funding_rate_status} ({'+' if funding_rate_bonus >= 0 else ''}{funding_rate_bonus * 100:.2f}点)\n"


    # 6. 流動性・構造分析サマリー
    analysis_detail += "\n**⚖️ 流動性・構造分析サマリー**\n"
    
    # 板の厚さ
    order_book_status = tech_data.get('order_book_status', 'Neutral')
    order_book_bonus = tech_data.get('order_book_bonus', 0.0)
    order_book_display = "🟢 Bid (買い板) 優位" if order_book_status == "Bid_Dominant" and side == "ロング" else \
                         ("🔴 Ask (売り板) 優位" if order_book_status == "Ask_Dominant" and side == "ショート" else "❌ 偏りなし")
    analysis_detail += f"   └ 板の厚さ: {order_book_display} ({'+' if order_book_bonus >= 0 else ''}{order_book_bonus * 100:.2f}点)\n"

    # VWAP近接
    vwap_val = tech_data.get('vwap_val', 0.0)
    vwap_bonus = tech_data.get('vwap_bonus', 0.0)
    analysis_detail += f"   └ VWAP近接: `{format_price_utility(vwap_val, symbol)}` ({'+' if vwap_bonus >= 0 else ''}{vwap_bonus * 100:.2f}点ボーナス)\n"

    return header + analysis_detail + trade_plan


# ====================================================================================
# MAIN LOOP & FastAPI SETUP
# ====================================================================================

async def main_loop():
    """ボットのメインループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, BTC_DOMINANCE_CONTEXT, EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    # 1. CCXTクライアントの初期化 (同期部分)
    if not EXCHANGE_CLIENT:
        EXCHANGE_CLIENT = initialize_ccxt_client(CCXT_CLIENT_NAME)
        if not EXCHANGE_CLIENT:
            logging.error("CCXTクライアントの初期化に失敗しました。ボットを停止します。")
            return

    # 2. マーケットリストの非同期ロード (OKXシンボルエラー対策)
    # これにより、CCXTが 'BTC/USDT' を 'BTC-USDT-SWAP' に正しくマッピングできるようになる
    try:
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"{CCXT_CLIENT_NAME.upper()} マーケットを正常にロードしました。")
    except Exception as e:
        logging.error(f"致命的: マーケットロードエラー ({CCXT_CLIENT_NAME}): {e}")
        return # マーケットロード失敗は致命的なので停止
    
    loop_count = 0

    while True:
        try:
            loop_count += 1
            logging.info(f"--- 🔄 Apex BOT {BOT_VERSION} 処理開始 (Loop: {loop_count}) ---")
            
            # 3. 監視銘柄リストの更新 (5分ごと、5ループに1回)
            if loop_count % 5 == 1: 
                dynamic_symbols = await fetch_dynamic_symbols()
                # 固定銘柄と動的銘柄を統合
                CURRENT_MONITOR_SYMBOLS = FIXED_SYMBOLS.union(dynamic_symbols)
                logging.info(f"監視銘柄リストを更新しました。固定:{len(FIXED_SYMBOLS)}, 動的:{len(dynamic_symbols)}, 総監視数: {len(CURRENT_MONITOR_SYMBOLS)}")

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
            
            for result_list in results:
                if result_list:
                    all_signals.extend(result_list)
            
            # 6. シグナルのフィルタリングとランキング (スコア 85点以上)
            high_conviction_signals = [
                s for s in all_signals 
                if s.get('score', 0.0) >= CONVICTION_SCORE_THRESHOLD
            ]
            
            ranked_signals = sorted(high_conviction_signals, key=lambda x: x.get('score', 0.0), reverse=True)
            
            LAST_ANALYSIS_SIGNALS = ranked_signals
            
            # 7. メッセージ生成とロギング
            messages = []
            for rank, signal in enumerate(ranked_signals[:5]): 
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank + 1)
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
                # 再初期化後、マーケットロードを再度試みる
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
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT {BOT_VERSION} is running."})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000)) 
    uvicorn.run(app, host="0.0.0.0", port=port)
