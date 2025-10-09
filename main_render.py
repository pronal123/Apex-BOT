# ====================================================================================
# Apex BOT v21.0.6 - Elliott/TSI/Ichimoku/OrderBook/FixedRRR Strategy
# - 新機能: エリオット波動プロキシ、TSI、一目均衡表、板情報バイアスによる詳細なスコアリングを追加。
# - 変更点: Dynamic Trailing Stop (DTS) から、固定R-R比 (4.1:1) に基づくTP/SLロジックに変更。
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

# BOTバージョン情報
BOT_VERSION = "v21.0.6 - Elliott/TSI/Ichimoku/OrderBook/FixedRRR"

# 取引所設定 (環境変数から読み込み)
CCXT_CLIENT_NAME = os.getenv("CCXT_CLIENT_NAME", "okx") # okx, binance, bybitなど
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
PASSWORD = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_PASSWORD") # OKXなどpassphraseが必要な場合

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "MATIC/USDT", "LINK/USDT", "BCH/USDT", "LTC/USDT"
]
TIME_FRAMES = ['1h', '4h'] # メイン分析は1h、トレンド確認は4h

# リスク管理と戦略の定数 (v21.0.6 Fixed RRR)
SL_ATR_MULTIPLIER = 2.5             # SLのATR倍率 (固定リスク幅の定義)
RRR_TARGET = 4.1                    # 目標リスクリワード比率 (RR: 4.1:1)
ATR_PERIOD = 14
ADX_THRESHOLD = 30.0                # 強いトレンドと判断するADXのしきい値
CONVICTION_SCORE_THRESHOLD = 0.85   # 高確度シグナルのしきい値

# スコアリングボーナス/ペナルティ定数 (v21.0.6 New Scoring)
ICHIMOKU_BONUS = 0.08               # 一目均衡表の優位性ボーナス (8.00点)
TSI_MOMENTUM_BONUS = 0.05           # TSIモメンタムボーナス (5.00点)
ELLIOTT_WAVE_BONUS = 0.12           # エリオット波動プロキシボーナス (12.00点)
ORDER_BOOK_BIAS_BONUS = 0.05        # 板情報バイアスボーナス (5.00点)
MTF_TREND_CONVERGENCE_BONUS = 0.10  # MTFトレンド収束ボーナス (10.00点)
VWAP_BONUS = 0.07                   # VWAP近接ボーナス (7.00点)
FR_BIAS_BONUS = 0.08                # 資金調達率優位性ボーナス (8.00点)
FR_PENALTY = -0.05                  # 資金調達率ペナルティ

# ====================================================================================
# GLOBAL STATE & INITIALIZATION
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

EXCHANGE_CLIENT = None
CURRENT_MONITOR_SYMBOLS = set(DEFAULT_SYMBOLS)
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_SUCCESS_TIME = 0.0
BTC_DOMINANCE_CONTEXT = {'trend': 'Neutral', 'value': 0.0}

# ====================================================================================
# CCXT & DATA ACQUISITION UTILITIES
# ====================================================================================

def initialize_ccxt_client(client_name: str) -> Optional[ccxt_async.Exchange]:
    """CCXTクライアントを初期化する"""
    try:
        if client_name.lower() == 'okx':
            client_class = getattr(ccxt_async, 'okx')
            params = {'password': PASSWORD}
        elif client_name.lower() == 'binance':
            client_class = getattr(ccxt_async, 'binanceusdm')
            params = {}
        elif client_name.lower() == 'bybit':
            client_class = getattr(ccxt_async, 'bybit')
            params = {}
        else:
            logging.error(f"未対応の取引所: {client_name}")
            return None

        client = client_class({
            'apiKey': API_KEY,
            'secret': SECRET,
            'password': PASSWORD,
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })
        logging.info(f"{client_name.upper()} クライアントを初期化しました。")
        return client
    except Exception as e:
        logging.error(f"CCXTクライアント初期化エラー: {e}")
        return None

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[Optional[pd.DataFrame], str, str]:
    """OHLCVデータを取得し、DataFrameに変換する。失敗した場合はフォールバックを試みる。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        EXCHANGE_CLIENT = initialize_ccxt_client(client_name)
        if not EXCHANGE_CLIENT:
            return None, "ExchangeError", client_name

    try:
        # OHLCVの取得
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
        # OKXは'swap'、Binanceは'future'
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        funding_rate = ticker.get('fundingRate', 0.0)
        return funding_rate if funding_rate is not None else 0.0
    except Exception as e:
        # logging.warning(f"FR取得エラー {symbol}: {e}")
        return 0.0

async def fetch_order_book_bias(symbol: str) -> Tuple[float, str]:
    """OKXから板情報を取得し、Bid/Askの厚さの偏りを計算する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, "Neutral"
    
    try:
        # スワップ市場のOrder Bookを取得 (OKX: instType='SWAP')
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=20, params={'instType': 'SWAP'})
        
        # トップN層のBid/Askのボリュームを比較
        limit = 5 # トップ5層のボリューム
        
        bid_volume = sum(bid[1] for bid in orderbook['bids'][:limit])
        ask_volume = sum(ask[1] for ask in orderbook['asks'][:limit])
        
        total_volume = bid_volume + ask_volume
        
        if total_volume == 0:
            return 0.0, "Neutral"
            
        # 偏り率を計算: (Bid - Ask) / Total
        bias_ratio = (bid_volume - ask_volume) / total_volume
        
        bias_status = "Neutral"
        # 5%以上の偏りを優位性ありと判断
        if bias_ratio > 0.05: 
            bias_status = "Bid_Dominant"
        elif bias_ratio < -0.05: 
            bias_status = "Ask_Dominant"
            
        return bias_ratio, bias_status
        
    except Exception as e:
        logging.warning(f"板情報取得エラー {symbol}: {e}")
        return 0.0, "Neutral"

async def get_crypto_macro_context() -> Dict:
    """BTCドミナンスの動向を取得する (v16.0.1のロジックを維持)"""
    try:
        # TradingViewのウィジェットAPIなどを利用するか、YFinanceでBTC/USDとTOTALCAPを比較するなど
        # ここでは簡易的にYFinanceでBTC/USDの直近のトレンドをプロキシとする
        btc_data = yf.download('BTC-USD', period='5d', interval='1h', progress=False)
        btc_data = btc_data.tail(100)
        btc_data.ta.ema(length=20, append=True)
        
        if len(btc_data) < 20:
            return {'trend': 'Neutral', 'value': 0.0}
            
        ema_now = btc_data['EMA_20'].iloc[-1]
        ema_prev = btc_data['EMA_20'].iloc[-5] # 5時間前のEMAと比較
        
        if ema_now > ema_prev * 1.0005: # 0.05%以上の増加
            trend = 'Long'
        elif ema_now < ema_prev * 0.9995: # 0.05%以上の減少
            trend = 'Short'
        else:
            trend = 'Neutral'
            
        current_price = btc_data['Close'].iloc[-1]
        
        return {'trend': trend, 'value': current_price}
    except Exception as e:
        logging.error(f"マクロコンテキスト取得エラー: {e}")
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
    v21.0.6では固定RRR (4.1:1) のTP/SLを計算。
    """
    
    # 1. データ取得とFunding Rate、Order Book取得
    ohlcv, status, _ = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    if status != "OK":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.0}
        
    df = ohlcv
    price = df['close'].iloc[-1]
    
    # Funding RateとOrder Bookはメインの1h足でのみ取得し、スコアリングに利用
    funding_rate_val = 0.0
    order_book_bias_ratio = 0.0
    order_book_status = "Neutral"

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
    
    # 💡 TSIと一目均衡表の追加
    df.ta.tsi(append=True) 
    df.ta.ichimoku(append=True)
    df.ta.ppo(append=True) # エリオット波動プロキシ用
    
    # 3. テクニカル指標の抽出
    atr_val = df[f'ATR_{ATR_PERIOD}'].iloc[-1]
    adx_val = df[f'ADX_{ATR_PERIOD}'].iloc[-1]
    rsi_val = df['RSI_14'].iloc[-1]
    
    # Ichimoku
    ichi_k_val = df['ICHI_K_9'].iloc[-1]     # 転換線
    ichi_t_val = df['ICHI_T_26'].iloc[-1]    # 基準線
    ichi_a_val = df['ICHI_A_26'].iloc[-1]    # 先行スパンA (雲の先行)
    ichi_b_val = df['ICHI_B_52'].iloc[-1]    # 先行スパンB (雲の先行)
    
    # TSI
    tsi_val = df['TSI_13_25_13'].iloc[-1]
    tsi_signal = df['TSIs_13_25_13'].iloc[-1] # pandas-taによるシグナル (-1, 0, 1)
    
    # Elliott Proxy
    macd_hist_val = df['MACDh_12_26_9'].iloc[-1]
    macd_hist_val_prev = df['MACDh_12_26_9'].iloc[-2]
    ppo_hist_val = df['PPOh_12_26_9'].iloc[-1]
    
    # Structural SL (Donchian Channel)
    dc_cols_present = 'DCU_20' in df.columns and 'DCL_20' in df.columns
    s1_pivot = df['DCL_20'].iloc[-2] if dc_cols_present else 0.0 # 過去20本の最安値(前日終値時点)
    r1_pivot = df['DCU_20'].iloc[-2] if dc_cols_present else 0.0 # 過去20本の最高値(前日終値時点)
    
    # VWAP
    vwap_val = df['VWAP'].iloc[-1] if 'VWAP' in df.columns else price
    
    # 4. トレンド方向の判定 (基本トレンドとスコアリングベース)
    side = "Neutral"
    if ichi_k_val > ichi_t_val and price > ichi_t_val: # 転換線 > 基準線 かつ 価格 > 基準線
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
        'long_term_trend': long_term_trend
    }
    
    # 5.1. ADX (トレンドの強さ) ベースのスコア
    regime = "レンジ/弱いトレンド"
    if adx_val >= ADX_THRESHOLD:
        regime = "強いトレンド"
        score += 0.1 # ベースボーナス
        
    # 5.2. RSI (買われすぎ/売られすぎ)
    if (side == "ロング" and rsi_val < 65) or (side == "ショート" and rsi_val > 35):
        score += 0.05
    
    # 5.3. Funding Rate Bias (優位性ボーナス 0.08 / ペナルティ -0.05)
    fr_bonus = 0.0
    if side == "ロング" and funding_rate_val < -0.0005: # マイナスFRでロング (優位性あり)
        fr_bonus = FR_BIAS_BONUS
    elif side == "ショート" and funding_rate_val > 0.0005: # プラスFRでショート (優位性あり)
        fr_bonus = FR_BIAS_BONUS
    elif side == "ロング" and funding_rate_val > 0.001: # 過密ペナルティ
        fr_bonus = FR_PENALTY
    elif side == "ショート" and funding_rate_val < -0.001: # 過密ペナルティ
        fr_bonus = FR_PENALTY
    score = min(1.0, score + fr_bonus)
    tech_data['funding_rate_bonus_value'] = fr_bonus
    
    # 5.4. VWAP近接ボーナス (0.07)
    vwap_bonus = 0.0
    if abs(price - vwap_val) / price < 0.001: # VWAPから0.1%以内
        vwap_bonus = VWAP_BONUS
    score = min(1.0, score + vwap_bonus)
    tech_data['vwap_bonus'] = vwap_bonus

    # 5.5. N. 一目均衡表優位性ボーナス (0.08)
    ichi_bonus = 0.0
    ichi_status = "Neutral"
    
    # 雲が将来的にLong方向 (A>B) かつ 価格が雲の上にあり、転換線が基準線の上
    if ichi_a_val > ichi_b_val and price > max(ichi_a_val, ichi_b_val) and ichi_k_val > ichi_t_val:
        if side == "ロング":
            ichi_bonus = ICHIMOKU_BONUS
            ichi_status = "Strong_Long"
    # 雲が将来的にShort方向 (A<B) かつ 価格が雲の下にあり、転換線が基準線の下
    elif ichi_a_val < ichi_b_val and price < min(ichi_a_val, ichi_b_val) and ichi_k_val < ichi_t_val:
        if side == "ショート":
            ichi_bonus = ICHIMOKU_BONUS
            ichi_status = "Strong_Short"

    score = min(1.0, score + ichi_bonus)
    tech_data['ichi_bonus'] = ichi_bonus
    tech_data['ichi_status'] = ichi_status

    # 5.6. M. TSIモメンタムボーナス (0.05)
    tsi_bonus = 0.0
    # TSIがゼロライン上で上向き (TSI > 5) または下向き (TSI < -5)
    if side == "ロング" and tsi_val > 5 and tsi_signal == 1: 
        tsi_bonus = TSI_MOMENTUM_BONUS
    elif side == "ショート" and tsi_val < -5 and tsi_signal == -1: 
        tsi_bonus = TSI_MOMENTUM_BONUS
    score = min(1.0, score + tsi_bonus)
    tech_data['tsi_bonus'] = tsi_bonus

    # 5.7. O. エリオット波動プロキシボーナス (0.12) - MACD Histの加速・減速を推進波プロキシとする
    elliott_bonus = 0.0
    elliott_status = "修正波/Neutral"
    
    # Long: MACD Histが増加し、PPO Histがゼロラインを越えて加速 (推進波プロキシ)
    if side == "ロング" and macd_hist_val > macd_hist_val_prev and ppo_hist_val > 0.0:
        elliott_bonus = ELLIOTT_WAVE_BONUS
        elliott_status = "推進波継続 (Wave 3/5)"
    # Short: MACD Histが減少し、PPO Histがゼロラインを割って加速 (推進波プロキシ)
    elif side == "ショート" and macd_hist_val < macd_hist_val_prev and ppo_hist_val < 0.0:
        elliott_bonus = ELLIOTT_WAVE_BONUS
        elliott_status = "推進波継続 (Wave 3/5)"

    score = min(1.0, score + elliott_bonus)
    tech_data['elliott_bonus'] = elliott_bonus
    tech_data['elliott_status'] = elliott_status

    # 5.8. P. 板情報 (Order Book) バイアスフィルター (0.05点)
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
    
    # エントリータイプの決定 (高確度シグナルはMarket/Limit, それ以外はLimit)
    use_limit_entry = score < 0.70 
    entry_type = "Limit" if use_limit_entry else "Market"

    # ATRベースの固定SL距離
    sl_dist_atr = atr_val * SL_ATR_MULTIPLIER 
    
    if side == "ロング":
        # Limitエントリーの場合、VWAPやBBAND下限などを参照してディスカウントエントリー
        if use_limit_entry:
            bb_low = df['BBL_20_2.0'].iloc[-1] if 'BBL_20_2.0' in df.columns else price
            entry = min(bb_low, price) 
        else:
            entry = price
        
        # SLの決定 (ATRベース)
        sl = entry - sl_dist_atr 
        
        # 構造的SL (S1) を使用する場合の調整（タイトで安全な場合のみ）
        if structural_sl_pivot > 0 and structural_sl_pivot > sl and structural_sl_pivot < entry:
             sl = structural_sl_pivot - atr_val * 0.5 # 構造的SLのバッファ修正
             
        if sl <= 0: sl = entry * 0.99 # ゼロ除算防止
        
        tp_dist = abs(entry - sl) * rr_base 
        tp1 = entry + tp_dist
        
        # 心理的SLは、主要な構造的サポートレベルを少し下回る位置に設定
        tech_data['psychological_sl'] = s1_pivot if s1_pivot > 0 and s1_pivot < entry else sl

        
    elif side == "ショート":
        # Limitエントリーの場合、BBAND上限などを参照してプレミアムエントリー
        if use_limit_entry:
            bb_high = df['BBU_20_2.0'].iloc[-1] if 'BBU_20_2.0' in df.columns else price
            entry = max(bb_high, price) 
        else:
            entry = price
        
        # SLの決定 (ATRベース)
        sl = entry + sl_dist_atr
        
        # 構造的SL (R1) を使用する場合の調整
        if structural_sl_pivot > 0 and structural_sl_pivot < sl and structural_sl_pivot > entry:
             sl = structural_sl_pivot + atr_val * 0.5 
             
        tp_dist = abs(entry - sl) * rr_base 
        tp1 = entry - tp_dist
        
        # 心理的SLは、主要な構造的レジスタンスレベルを少し上回る位置に設定
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
    long_term_trend = 'Neutral'
    # 4h分析はMTFトレンド収束チェックにのみ使用するため、スコアは計算しない
    h4_analysis = await analyze_single_timeframe(client_name, symbol, '4h')
    if h4_analysis and h4_analysis.get('side') in ['ロング', 'ショート']:
        long_term_trend = h4_analysis['side']

    # 1. メイン時間軸の分析 (1h) を実行
    results: List[Optional[Dict]] = []
    
    h1_analysis = await analyze_single_timeframe(client_name, symbol, '1h', long_term_trend)
    results.append(h1_analysis)
    
    # 2. MTF スコアリングブーストロジック & トレンド収束ボーナス
    valid_signals = [r for r in results if r and r.get('side') in ['ロング', 'ショート']]

    for result in valid_signals:
        side = result['side']
        score = result['score']
        
        # BTCドミナンスバイアスを適用 (Altcoinのみ)
        dominance_bias_bonus_value = 0.0
        if symbol != "BTC/USDT":
            if (side == 'ロング' and macro_context['trend'] == 'Short') or \
               (side == 'ショート' and macro_context['trend'] == 'Long'):
                dominance_bias_bonus_value = 0.0 # BTCが逆行の場合、ボーナスなし (Altの優位性が低い)
            else:
                dominance_bias_bonus_value = macro_context.get('dominance_bias_bonus', 0.0)
        
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
        
    return [r for r in results if r is not None]

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
    
    # 1. 最も高スコアのシグナルを選定 (1h足)
    high_score_signals = [s for s in signals if s.get('score', 0.0) >= CONVICTION_SCORE_THRESHOLD and s.get('side') in ['ロング', 'ショート']]
    
    if not high_score_signals:
        return ""
        
    best_signal = sorted(high_score_signals, key=lambda x: x.get('score', 0.0), reverse=True)[0]

    # 2. 主要な取引情報を抽出
    display_symbol = symbol.replace('/USDT', '')
    timeframe = best_signal.get('timeframe', '1h')
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
    
    # 損失/利益率の計算 (簡易的な例, エントリー価格に対する比率)
    risk_pct = sl_width / entry_price * 100
    reward_pct = reward_usd / entry_price * 100
    
    # 勝率の仮定 (スコアベース)
    win_rate = 50.0 + (score_100 - 50.0) * 0.7 # 50点をベースに50点以上の超過分を70%で反映

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
        f"📊 **現在の価格**: {format_price_utility(price, symbol)}\n"
        f"🎯 **エントリー ({entry_type})**: {format_price_utility(entry_price, symbol)}\n"
        f"🚫 **損切り (SL)**: {format_price_utility(sl_price, symbol)} (心理的SL: {format_price_utility(psychological_sl, symbol)})\n"
        f"✅ **利確 (TP)**: {format_price_utility(tp_price, symbol)}\n"
        f"\n**📝 PnL予測**\n"
        f"----------------------------------\n"
        f"└ 予想損失 (SL): -{format_price_utility(sl_width, symbol)} USD (-{risk_pct:.2f}%)\n"
        f"└ 予想利益 (TP): +{format_price_utility(reward_usd, symbol)} USD (+{reward_pct:.2f}%)\n"
    )

    # 5. 統合分析サマリー
    analysis_detail = "\n**🔬 統合分析サマリー (1h軸)**\n"
    
    # 💡 MTFトレンド収束
    mtf_bonus = tech_data.get('mtf_convergence_bonus', 0.0)
    mtf_trend_str = "❌ トレンド不一致/Neutral"
    if mtf_bonus > 0:
        mtf_trend_str = "✨ MTFトレンド収束: 4h軸トレンド一致！"
    analysis_detail += (
        f"{mtf_trend_str} (+{mtf_bonus * 100:.2f}点 ボーナス！)\n"
    )

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
    analysis_detail += f"   └ VWAP近接: {format_price_utility(vwap_val, symbol)} ({'+' if vwap_bonus >= 0 else ''}{vwap_bonus * 100:.2f}点ボーナス)\n"

    return header + analysis_detail + trade_plan


# ====================================================================================
# MAIN LOOP & FastAPI SETUP
# ====================================================================================

async def main_loop():
    """ボットのメインループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, BTC_DOMINANCE_CONTEXT
    
    # CCXTクライアントの初期化
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        EXCHANGE_CLIENT = initialize_ccxt_client(CCXT_CLIENT_NAME)
        if not EXCHANGE_CLIENT:
            logging.error("CCXTクライアントの初期化に失敗しました。ボットを停止します。")
            return

    while True:
        try:
            logging.info(f"--- 🔄 Apex BOT {BOT_VERSION} 処理開始 ---")
            
            # 1. マクロコンテキストの取得
            BTC_DOMINANCE_CONTEXT = await get_crypto_macro_context()
            logging.info(f"マクロコンテキスト (BTCトレンド): {BTC_DOMINANCE_CONTEXT['trend']}")

            # 2. 監視銘柄の分析
            all_signals: List[Dict] = []
            tasks = []
            
            # 現在の監視リストをシャッフル
            symbols_to_monitor = list(CURRENT_MONITOR_SYMBOLS)
            random.shuffle(symbols_to_monitor)

            for symbol in symbols_to_monitor:
                tasks.append(generate_integrated_signal(symbol, BTC_DOMINANCE_CONTEXT, CCXT_CLIENT_NAME))

            results = await asyncio.gather(*tasks)
            
            for result_list in results:
                if result_list:
                    all_signals.extend(result_list)
            
            # 3. シグナルのフィルタリングとランキング
            # 高確度シグナルのみを抽出 (スコア 85点以上)
            high_conviction_signals = [
                s for s in all_signals 
                if s.get('score', 0.0) >= CONVICTION_SCORE_THRESHOLD
            ]
            
            # スコア降順でソート
            ranked_signals = sorted(high_conviction_signals, key=lambda x: x.get('score', 0.0), reverse=True)
            
            LAST_ANALYSIS_SIGNALS = ranked_signals
            
            # 4. メッセージ生成
            messages = []
            for rank, signal in enumerate(ranked_signals[:5]): # TOP 5シグナルを出力
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank + 1)
                messages.append(message)
                
            # メッセージをログに出力 (実際の配信は外部サービスを想定)
            if messages:
                logging.info(f"--- 📣 高確度シグナル (TOP {len(messages)}) ---\n" + "\n\n".join(messages))
            else:
                logging.info("高確度シグナルは見つかりませんでした。")

            LAST_SUCCESS_TIME = time.time()
            logging.info(f"--- ✅ Apex BOT {BOT_VERSION} 処理完了 (次まで60秒待機) ---")
            
            # 1分間待機して次のループへ
            await asyncio.sleep(60)

        except Exception as e:
            error_name = type(e).__name__
            
            if "Connection reset by peer" in str(e):
                logging.warning("接続リセットエラー。CCXTクライアントを再初期化します。")
                global EXCHANGE_CLIENT
                if EXCHANGE_CLIENT:
                    await EXCHANGE_CLIENT.close()
                EXCHANGE_CLIENT = initialize_ccxt_client(CCXT_CLIENT_NAME)
            
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
    # 環境変数にポートが指定されていなければ8000を使用
    port = int(os.getenv("PORT", 8000)) 
    uvicorn.run(app, host="0.0.0.0", port=port)
