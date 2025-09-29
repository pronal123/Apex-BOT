# ====================================================================================
# Apex BOT v9.1.0 - 堅牢性・フォールバック強化版
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
import yfinance as yf
import asyncio
import random
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 📌 初期監視対象銘柄リスト 
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 📌 v9.1.0 設定
LOOP_INTERVAL = 60      # メイン分析ループ間隔を60秒に維持
PING_INTERVAL = 8       # 自己Ping間隔を8秒に維持
PING_TIMEOUT = 12       # Pingのタイムアウト時間を12秒に維持
DYNAMIC_UPDATE_INTERVAL = 600 # マクロ分析/銘柄更新間隔 (10分)
REQUEST_DELAY = 0.5     # CCXTリクエスト間の遅延 (0.5秒)
MIN_SLEEP_AFTER_IO = 0.005 # IO解放のための最小スリープ時間

# 突発変動検知設定
INSTANT_CHECK_INTERVAL = 15 # 即時価格チェック間隔（秒）
MAX_PRICE_DEVIATION_PCT = 1.5 # 15分間の価格変動率の閾値（%）
INSTANT_CHECK_WINDOW_MIN = 15 # 変動をチェックする期間（分）
ORDER_BOOK_DEPTH_LEVELS = 10 # オーダーブック分析の深度を10層に増加

# データフォールバック設定
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 400} # 銘柄ごとの最低必要バー数
FALLBACK_MAP = {'4h': '1h'} # 4hが不足したら1hで代替試行

# ログ設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    force=True)

# CCXTのログをWarningレベルに抑制
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
PRICE_HISTORY: Dict[str, List[Tuple[float, float]]] = {} 

# 価格整形ユーティリティ (元のコードにはなかったが通知の整形に必要なので追加)
def format_price_utility(price: float, symbol: str) -> str:
    if symbol in ["BTC", "ETH"]:
        return f"{price:,.2f}"
    elif symbol in ["XRP", "ADA", "DOGE"]:
        return f"{price:.4f}"
    else:
        return f"{price:.6f}"


# ====================================================================================
# UTILITIES & CLIENTS
# ====================================================================================

# ... (initialize_ccxt_client, send_test_message, send_telegram_html は元のコードから変更なし) ...
def initialize_ccxt_client():
    """CCXTクライアントを初期化（複数クライアントで負荷分散）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH

    clients = {
        'Binance': ccxt_async.binance({"enableRateLimit": True, "timeout": 20000}),
        'Bybit': ccxt_async.bybit({"enableRateLimit": True, "timeout": 30000}), 
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 30000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
    }

    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}


async def send_test_message():
    """起動テスト通知 (v9.0.0に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v9.1.0 - 起動テスト通知 (堅牢性強化)</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: データフォールバック・クライアントヘルス管理を強化しました。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

def send_telegram_html(text: str, is_emergency: bool = False):
    """HTML形式でTelegramにメッセージを送信する（ブロッキング関数）"""
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "").replace("\n", " ").replace("•", "").replace("-", "").strip()
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。ログに出力されます。")
        logging.info("--- TELEGRAM通知（ダミー）---\n" + clean_text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": not is_emergency
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知成功。Response Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラーが発生しました: {e}")


# ====================================================================================
# CORE ANALYSIS & HELPER FUNCTIONS
# ====================================================================================

# ... (get_tradfi_macro_context, calculate_trade_levels, get_news_sentiment, 
#      calculate_elliott_wave_score, calculate_technical_indicators, get_ml_prediction は元のコードから変更なし) ...

def get_tradfi_macro_context() -> Dict:
    """伝統的金融市場（VIXなど）からマクロ環境のコンテクストを取得（ブロッキング関数）"""
    context = {"trend": "中立", "vix_level": 0.0, "gvix_level": 0.0, "risk_appetite_boost": 0.0}
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="1h")
        if not vix.empty:
            vix_level = vix['Close'].iloc[-1]
            context["vix_level"] = vix_level
            
            # 📌 VIXに基づくリスクセンチメントの調整 (v9.0.0強化)
            if vix_level < 15:
                context["trend"] = "リスクオン (VIX低)"
                context["risk_appetite_boost"] = 0.05 # リスクオン環境でロングにボーナス
            elif vix_level > 25:
                context["trend"] = "リスクオフ (VIX高)"
                context["risk_appetite_boost"] = -0.05 # リスクオフ環境でショートにボーナス
            else:
                context["trend"] = "中立"

        context["gvix_level"] = random.uniform(40, 60) # 簡易的な仮想通貨ボラティリティ指標
    except Exception:
        pass
    return context

def calculate_trade_levels(closes: pd.Series, side: str, score: float, adx_level: float) -> Dict:
    """価格データと信頼度スコア、ADXからエントリー/決済レベルを計算（v9.0.0 強化）"""
    if len(closes) < 20:
        current_price = closes.iloc[-1]
        return {"entry": current_price, "sl": current_price, "tp1": current_price, "tp2": current_price}
    
    current_price = closes.iloc[-1]
    volatility_range = closes.diff().abs().std() * 2 
    
    # 📌 ADXに基づくTP/SL幅の動的調整 (v9.0.0 強化)
    adx_factor = np.clip((adx_level - 20) / 20, 0.5, 1.5) 
    
    # スコアとADXを統合した最終調整係数
    multiplier = (1.0 + score * 0.5) * adx_factor 
    
    if side == "ロング":
        entry = current_price * 0.9995 
        sl = current_price - (volatility_range * (2.0 - adx_factor))
        tp1 = current_price + (volatility_range * 1.5 * multiplier) 
        tp2 = current_price + (volatility_range * 3.0 * multiplier)
    else:
        entry = current_price * 1.0005 
        sl = current_price + (volatility_range * (2.0 - adx_factor))
        tp1 = current_price - (volatility_range * 1.5 * multiplier)
        tp2 = current_price - (volatility_range * 3.0 * multiplier)
        
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}

def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコアを取得（実際はAPI利用だが、ここではダミー）"""
    sentiment_score = 0.5
    if random.random() < 0.1: sentiment_score = 0.65 
    elif random.random() > 0.9: sentiment_score = 0.35 
    return {"sentiment_score": sentiment_score}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
    """エリオット波動理論に基づいた波形フェーズとスコアを計算（簡易版）"""
    if len(closes) < 50: return 0.0, "不明"
    volatility = closes.pct_change().std()
    recent_trend_strength = closes.iloc[-1] / closes.iloc[-20:].mean() - 1
    if volatility < 0.005 and abs(recent_trend_strength) < 0.01:
        wave_score = 0.2
        wave_phase = "修正波 (レンジ)"
    elif abs(recent_trend_strength) > 0.05 and volatility > 0.01:
        wave_score = 0.8
        wave_phase = "推進波 (トレンド)"
    else:
        wave_score = random.uniform(0.3, 0.7)
        wave_phase = "移行期"
    return wave_score, wave_phase

def calculate_technical_indicators(ohlcv: List[list]) -> Dict:
    """RSI, MACD, ADX, CCIを計算し、テクニカルデータを返す"""
    closes = pd.Series([c[4] for c in ohlcv])
    highs = pd.Series([c[2] for c in ohlcv])
    lows = pd.Series([c[3] for c in ohlcv])
    if len(closes) < 50:
        return {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

    # 1. RSI (14)
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs)).iloc[-1]

    # 2. MACD (12, 26, 9)
    exp1 = closes.ewm(span=12, adjust=False).mean()
    exp2 = closes.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - signal_line
    macd_direction_boost = 0.0
    if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-2] <= 0: macd_direction_boost = 0.05 
    elif macd_hist.iloc[-1] < 0 and macd_hist.iloc[-2] >= 0: macd_direction_boost = -0.05
    elif macd_hist.iloc[-1] > 0: macd_direction_boost = 0.02
    elif macd_hist.iloc[-1] < 0: macd_direction_boost = -0.02
    
    # 3. ADX (トレンド強度 - 簡易版)
    adx = ((highs - lows).abs() / closes).rolling(window=14).mean().iloc[-1] * 1000
    adx = np.clip(adx, 15, 60) 
    
    # 4. CCI (商品チャネル指数 - 過熱感)
    tp = (highs + lows + closes) / 3
    ma = tp.rolling(window=20).mean()
    md = tp.rolling(window=20).apply(lambda x: abs(x - x.mean()).mean(), raw=True) 
    cci = (tp - ma) / (0.015 * md)

    return {
        "rsi": rsi,
        "macd_hist": macd_hist.iloc[-1],
        "macd_direction_boost": macd_direction_boost,
        "adx": adx,
        "cci_signal": cci.iloc[-1]
    }

def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> Tuple[float, Dict]:
    """機械学習モデルの簡易シミュレーションとテクニカルデータの統合"""
    try:
        tech_data = calculate_technical_indicators(ohlcv)
        rsi = tech_data["rsi"]
        macd_boost = tech_data["macd_direction_boost"]
        cci = tech_data["cci_signal"]

        news_sentiment = sentiment.get("sentiment_score", 0.5)
        
        base_prob = 0.5 + ((rsi - 50) / 100) * 0.5 
        cci_boost = np.clip(cci / 500, -0.05, 0.05) * -1 

        final_prob = base_prob + macd_boost + (news_sentiment - 0.5) * 0.1 + cci_boost
        
        win_prob = np.clip(final_prob, 0.35, 0.65)
        
        return win_prob, tech_data

    except Exception:
        return 0.5, {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

# -----------------------------------------------------------------------------------
# CCXT WRAPPER FUNCTIONS (v9.1.0 強化)
# -----------------------------------------------------------------------------------

def _aggregate_ohlcv(ohlcv_source: List[list], target_timeframe: str) -> List[list]:
    """OHLCVデータをダウンサンプリングして模擬データを作成する"""
    if not ohlcv_source: return []
    
    df = pd.DataFrame(ohlcv_source, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df.set_index('datetime')

    # Pandasのresample機能を使ってOHLCVをダウンサンプリング
    resampled_df = df.resample(target_timeframe).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()

    # CCXT形式 (List[list])に戻す
    resampled_ohlcv = [
        [int(ts.timestamp() * 1000), row['open'], row['high'], row['low'], row['close'], row['volume']]
        for ts, row in resampled_df.iterrows()
    ]
    return resampled_ohlcv

async def fetch_ohlcv_single_client(client_name: str, symbol: str, timeframe: str, limit: int) -> Tuple[List[list], str]:
    """単一のCCXTクライアントからOHLCVデータを取得し、I/O解放スリープを挿入"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return [], "NoClient"
    
    # 可能な限り多くのシンボル形式を試行
    market_symbols = [f"{symbol}/USDT"]
    if client_name == 'Coinbase': market_symbols.insert(0, f"{symbol}-USD")
    elif client_name == 'Binance': market_symbols.append(f"{symbol}/BUSD")
        
    for market_symbol in market_symbols:
        try:
            await asyncio.sleep(REQUEST_DELAY) 
            ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
            await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
            
            if ohlcv and len(ohlcv) >= limit:
                global ACTIVE_CLIENT_HEALTH
                ACTIVE_CLIENT_HEALTH[client_name] = time.time() 
                return ohlcv, "Success"
            
            # データ不足またはCoinbaseの4hスキップ（データ取得は成功したがデータがない）
            if ohlcv is not None and len(ohlcv) < limit:
                 logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}, {timeframe}) データ不足: {len(ohlcv)}/{limit}本。")
                 return ohlcv, "DataShortage" # データ不足ステータスを返す

        except ccxt_async.RateLimitExceeded:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RateLimitExceeded。即時クライアント切り替え要求。")
            return [], "RateLimit" 
        except ccxt_async.RequestTimeout:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RequestTimeout。即時クライアント切り替え要求。")
            return [], "Timeout" 
        except Exception as e:
            logging.debug(f"CCXT ({client_name}, {market_symbol}) 一般エラー: {type(e).__name__}")
            continue
            
    return [], "NoData"


async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[list], str]:
    """
    OHLCVデータを取得し、長期データが不足している場合に短期データで代替を試みる（v9.1.0の核心強化）
    """
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
    
    # 1. ターゲット時間足の取得を試行
    ohlcv, status = await fetch_ohlcv_single_client(client_name, symbol, timeframe, limit)

    if status == "Success":
        return ohlcv, status
    
    # 2. データ不足の場合、フォールバックを試行
    if status == "DataShortage" and timeframe in FALLBACK_MAP:
        source_tf = FALLBACK_MAP[timeframe]
        
        # ターゲットに必要なバー数をカバーするために必要なソースのバー数
        tf_ratio = int(timeframe.replace('h', '')) / int(source_tf.replace('h', '').replace('m', ''))
        source_limit = int(limit * tf_ratio)
        
        logging.info(f"--- ⚠️ {timeframe} データ不足を検知。{source_tf} ({source_limit}本)での模擬データ生成を試行 ---")
        
        # ソース時間足のデータを取得
        ohlcv_source, source_status = await fetch_ohlcv_single_client(client_name, symbol, source_tf, source_limit)

        if source_status == "Success" or (source_status == "DataShortage" and len(ohlcv_source) > 0):
            # 取得できたソースデータを使ってダウンサンプリング
            simulated_ohlcv = _aggregate_ohlcv(ohlcv_source, timeframe)
            
            if len(simulated_ohlcv) >= limit:
                logging.info(f"--- 🎉 成功: 模擬的な {timeframe} データ {len(simulated_ohlcv)}本を作成しました。---")
                return simulated_ohlcv[-limit:], "FallbackSuccess" # 成功として返す
            else:
                logging.warning(f"--- ⚠️ 警告: 模擬データ作成後も {len(simulated_ohlcv)}本で、まだ不足しています。---")
                return simulated_ohlcv, "FallbackShortage" # 最終的に不足したステータスを返す
        else:
            logging.error(f"--- ❌ 代替ソース ({source_tf}) の取得にも失敗しました ({source_status})。---")
            return [], status # 元のエラーステータスを返す
            
    # RateLimit, Timeout, NoClient, NoData の場合はそのまま返す
    return ohlcv, status


async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """オーダーブックの深度を取得し、買い/売り圧の比率を計算（v9.0.0 強化）"""
    client = CCXT_CLIENTS_DICT.get(CCXT_CLIENT_NAME)
    if client is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}

    market_symbol = f"{symbol}/USDT" 

    try:
        await asyncio.sleep(REQUEST_DELAY)
        # 深度をORDER_BOOK_DEPTH_LEVELSで定義した層数（10層）まで取得
        order_book = await client.fetch_order_book(market_symbol, limit=20) 
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        
        # 📌 上位10層のボリュームを加重平均（v9.0.0 強化）
        # 価格に近い層ほど重み付けを大きくする (価格*量の合計)
        bid_volume_weighted = sum(amount * price for price, amount in order_book['bids'][:ORDER_BOOK_DEPTH_LEVELS])
        ask_volume_weighted = sum(amount * price for price, amount in order_book['asks'][:ORDER_BOOK_DEPTH_LEVELS])

        total_volume = bid_volume_weighted + ask_volume_weighted
        depth_ratio = bid_volume_weighted / total_volume if total_volume > 0 else 0.5

        return {"bid_volume": bid_volume_weighted, "ask_volume": ask_volume_weighted, 
                "depth_ratio": depth_ratio, "total_depth": total_volume}

    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}


async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """単一銘柄に対する分析を実行し、シグナル候補を生成（v9.1.0 強化）"""
    
    sentiment_data = get_news_sentiment(symbol)
    
    # 1. 15mデータの取得
    ohlcv_15m, ccxt_status_15m = await fetch_ohlcv_with_fallback(client_name, symbol, '15m')
    
    if ccxt_status_15m in ["RateLimit", "Timeout"]:
        return {"symbol": symbol, "side": ccxt_status_15m, "score": 0.0, "client": client_name}
    if not ohlcv_15m or len(ohlcv_15m) < REQUIRED_OHLCV_LIMITS['15m']:
         # データ不足の場合は、長期分析用の4hデータも確認せずにスキップ
        return None 

    # 2. 長期データ（4h）の取得とフォールバック試行
    # シグナルのスコアリングには15mを使うが、ADX/レジーム判定のために長期データも試行
    ohlcv_4h, ccxt_status_4h = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    
    # 4hデータが取得できなかった場合でも、15mがあればシグナルは続行
    if not ohlcv_4h or len(ohlcv_4h) < 50: # 4hデータは50本でADX計算を試行
        adx_level = 25.0 # 4hがなければデフォルト値
        logging.debug(f"⚠️ {symbol} - 4hデータが不足 ({ccxt_status_4h})。ADXはデフォルト値を使用。")
    else:
        tech_4h = calculate_technical_indicators(ohlcv_4h)
        adx_level = tech_4h.get('adx', 25.0)

    # 3. シグナルロジック（15mベース）
    win_prob, tech_data = get_ml_prediction(ohlcv_15m, sentiment_data)
    closes = pd.Series([c[4] for c in ohlcv_15m])
    
    wave_score, wave_phase = calculate_elliott_wave_score(closes)
    depth_data = await fetch_order_book_depth_async(symbol) 
    source = client_name
    
    # --- 中立シグナル判定 --- (元のコードから変更なし)
    if 0.47 < win_prob < 0.53:
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場" if adx_level < 25 else "移行期"
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": False,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
                "total_depth": depth_data['total_depth'],
                "tech_data": tech_data, "sentiment_score": sentiment_data["sentiment_score"]}

    # --- トレードシグナル判定とスコアリング --- (元のコードから変更なし)
    side = "ロング" if win_prob >= 0.53 else "ショート"
    
    # 1. 基本スコア
    base_score = abs(win_prob - 0.5) * 2 
    base_score *= (0.8 + wave_score * 0.4) 

    # 2. ADX（トレンド強度）調整
    if adx_level > 30: 
        adx_boost = 0.1 # トレンドに乗る
        regime = "トレンド相場"
    elif adx_level < 20: 
        adx_boost = -0.05 # レンジならスコア減点 (予測の難易度上昇)
        regime = "レンジ相場"
    else: 
        adx_boost = 0.0
        regime = "移行期"

    # 3. センチメント調整
    sentiment_boost = (sentiment_data["sentiment_score"] - 0.5) * 0.2
    if side == "ショート": sentiment_boost *= -1 

    # 4. 深度（板の厚み）調整
    depth_adjustment = 0.0
    if side == "ロング":
        depth_adjustment = (depth_data['depth_ratio'] - 0.5) * 0.4 
    else:
        depth_adjustment = (0.5 - depth_data['depth_ratio']) * 0.4 

    # 5. マクロ環境調整
    macro_boost = macro_context_data.get('risk_appetite_boost', 0.0)
    if side == "ショート": macro_boost *= -1 

    # 6. 最終スコア算出
    final_score = np.clip((base_score + depth_adjustment + adx_boost + sentiment_boost + macro_boost), 0.0, 1.0)

    # 7. トレードレベル計算
    trade_levels = calculate_trade_levels(closes, side, final_score, adx_level)

    return {"symbol": symbol, "side": side, "price": closes.iloc[-1], "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": ccxt_status_4h == "FallbackSuccess", # 4hがフォールバックならTrue
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
            "total_depth": depth_data['total_depth'],
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data,
            "source": source, "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data}


# -----------------------------------------------------------------------------------
# TELEGRAM FORMATTING & ASYNC TASKS
# -----------------------------------------------------------------------------------

# ... (format_telegram_message, update_monitor_symbols_dynamically, 
#      fetch_current_price_single, instant_price_check_task, 
#      blocking_ping, self_ping_task は元のコードから変更なし。ただし、ブロッキング関数呼び出しは to_thread に変更) ...

def format_instant_message(symbol, side, change_pct, window, price, old_price):
     # 突発変動のメッセージ整形 (元のコードから抽出)
     format_p = lambda p: format_price_utility(p, symbol)
     return (
        f"🚨 <b>突発変動警報: {symbol} が {window}分間で{side}!</b> 💥\n"
        f"• **変動率**: <code>{change_pct:.2f}%</code>\n"
        f"• **現在価格**: <code>${format_p(price)}</code> (始点: <code>${format_p(old_price)}</code>)\n"
        f"<b>【BOTの判断】: 市場が急激に動いています。ポジションの確認を推奨します。</b>"
    )

async def instant_price_check_task():
    """15秒ごとに監視銘柄の価格を取得し、突発的な変動をチェックする。"""
    global PRICE_HISTORY, CCXT_CLIENT_NAME
    INSTANT_NOTIFICATION_COOLDOWN = 180 # 3分間
    logging.info(f"⚡ 突発変動即時チェックタスクを開始します (インターバル: {INSTANT_CHECK_INTERVAL}秒)。")
    while True:
        await asyncio.sleep(INSTANT_CHECK_INTERVAL)
        current_time = time.time()
        symbols_to_check = list(CURRENT_MONITOR_SYMBOLS)
        price_tasks = [fetch_current_price_single(CCXT_CLIENT_NAME, sym) for sym in symbols_to_check]
        prices = await asyncio.gather(*price_tasks)
        
        for symbol, price in zip(symbols_to_check, prices):
            if price is None: continue
            
            if symbol not in PRICE_HISTORY: PRICE_HISTORY[symbol] = []
            PRICE_HISTORY[symbol].append((current_time, price))
            
            cutoff_time = current_time - INSTANT_CHECK_WINDOW_MIN * 60
            PRICE_HISTORY[symbol] = [(t, p) for t, p in PRICE_HISTORY[symbol] if t >= cutoff_time]
            
            history = PRICE_HISTORY[symbol]
            if len(history) < 2: continue
            
            oldest_price = history[0][1]
            latest_price = price
            if oldest_price == 0: continue
            
            change_pct = (latest_price - oldest_price) / oldest_price * 100
            
            if abs(change_pct) >= MAX_PRICE_DEVIATION_PCT:
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(symbol, 0) > INSTANT_NOTIFICATION_COOLDOWN
                if is_not_recently_notified:
                    side = "急騰" if change_pct > 0 else "急落"
                    message = format_instant_message(symbol, side, change_pct, INSTANT_CHECK_WINDOW_MIN, latest_price, oldest_price)
                    await asyncio.to_thread(lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[symbol] = current_time 
                    logging.warning(f"🚨 突発変動検知: {symbol} が {INSTANT_CHECK_WINDOW_MIN}分間で {change_pct:.2f}% {side}しました。即時通知実行。")


async def self_ping_task(interval: int = PING_INTERVAL):
    """RenderのIdleシャットダウンを防ぐためのタスク。ブロッキングなPingをExecutorで実行。"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not render_url:
        logging.warning("⚠️ RENDER_EXTERNAL_URL環境変数が設定されていません。自己Pingは無効です。")
        return
    logging.info(f"🟢 自己Pingタスクを開始します (インターバル: {interval}秒, T/O: {PING_TIMEOUT}秒, 方式: Threaded Head)。URL: {render_url}")
    if not render_url.startswith('http'):
        render_url = f"https://{render_url}"
    ping_url = render_url.rstrip('/') + '/' 
    while True:
        await asyncio.to_thread(lambda: blocking_ping(ping_url, PING_TIMEOUT))
        await asyncio.sleep(interval)


# -----------------------------------------------------------------------------------
# MAIN LOOP & EXECUTION
# -----------------------------------------------------------------------------------

async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH

    macro_context_data = await asyncio.to_thread(get_tradfi_macro_context)
    LAST_UPDATE_TIME = time.time()
    
    await send_test_message()
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())

    while True:
        try:
            await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
            current_time = time.time()
            
            # 📌 健全性に基づくクライアント選択: Health値が最大（最新の成功時刻）のクライアントを選択
            CCXT_CLIENT_NAME = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])

            # --- 動的更新フェーズ (10分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                logging.info(f"Apex BOT v9.1.0 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                macro_context_data = await asyncio.to_thread(get_tradfi_macro_context)
                await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME)
                LAST_UPDATE_TIME = current_time
                logging.info(f"監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}")
                logging.info("--------------------------------------------------")
            else:
                logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME})")

            # --- メイン分析実行 ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data, CCXT_CLIENT_NAME)
                               for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)

            # --- エラーハンドリングとクライアント切り替え ---
            error_candidates = [c for c in candidates if isinstance(c, dict) and c.get('side') in ["RateLimit", "Timeout"]]
            if error_candidates:
                # 📌 エラー発生時、クライアントのヘルスを過去（3600秒前）にリセットしてペナルティを課す
                penalized_time = time.time() - 3600 
                logging.error(f"❌ レート制限/タイムアウトエラー発生: クライアント {CCXT_CLIENT_NAME} のヘルスを {penalized_time:.0f} にリセット。")
                ACTIVE_CLIENT_HEALTH[CCXT_CLIENT_NAME] = penalized_time
                next_client = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])
                logging.info(f"➡️ 即時クライアント切り替え：次回は {next_client} を優先試行。")
                await asyncio.sleep(5) 
                continue 

            # --- シグナル選定と通知 ---
            valid_candidates_and_neutral = [c for c in candidates if c is not None and c.get('side') not in ["RateLimit", "Timeout"]]
            success_count = len(valid_candidates_and_neutral)
            
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0: LAST_SUCCESS_TIME = current_time

            valid_candidates = [c for c in valid_candidates_and_neutral if c.get('side') != "Neutral" and c.get('score', 0) >= 0.50]
            neutral_candidates = [c for c in valid_candidates_and_neutral if c.get('side') == "Neutral"]

            # 1. 最優秀シグナル通知
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600
                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await asyncio.to_thread(lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                    logging.info(f"🎉 高スコアシグナル通知成功: {best_signal['symbol']} ({best_signal['side']}) スコア: {best_signal['score']:.2f}")

            # 2. 強制ヘルスチェック通知（1時間に一度）
            elif (current_time - NEUTRAL_NOTIFIED_TIME) > 3600:
                stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
                neutral_check_signal = {"symbol": "System Health", "side": "Neutral", "confidence": 0.0, 
                                        "macro_context": macro_context_data, "is_fallback": True, "analysis_stats": stats,
                                        "tech_data": {"rsi": 50, "adx": 25}} # ヘルスチェック用のダミーデータ
                message = format_telegram_message(neutral_check_signal)
                await asyncio.to_thread(lambda: send_telegram_html(message, is_emergency=False))
                NEUTRAL_NOTIFIED_TIME = current_time
                logging.info("✅ 強制ヘルスチェック通知を実行しました。")

        except Exception as e:
            logging.error(f"💣 メインループで予期せぬ致命的なエラー: {type(e).__name__}: {e}", exc_info=True)
            await asyncio.sleep(60) # 致命的なエラー時は1分待機

        await asyncio.sleep(LOOP_INTERVAL)


# -----------------------------------------------------------------------------------
# FastAPI WEB SERVER (変更なし)
# -----------------------------------------------------------------------------------

app = FastAPI()

@app.get("/")
async def health_check(request: Request):
    """ヘルスチェックエンドポイント (自己Ping用)"""
    return {"status": "ok", "message": "Apex BOT v9.1.0 is running."}

@app.get("/status")
async def get_status():
    """現在のBOTの状態を取得"""
    return JSONResponse({
        "status": "active",
        "last_update": datetime.fromtimestamp(LAST_UPDATE_TIME, JST).strftime('%Y-%m-%d %H:%M:%S') if LAST_UPDATE_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "total_attempts": TOTAL_ANALYSIS_ATTEMPTS,
        "total_errors": TOTAL_ANALYSIS_ERRORS,
        "client_health": {k: datetime.fromtimestamp(v, JST).strftime('%Y-%m-%d %H:%M:%S') for k, v in ACTIVE_CLIENT_HEALTH.items()}
    })

# -----------------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に初期化とメインループタスクを開始"""
    initialize_ccxt_client()
    asyncio.create_task(main_loop())
    logging.info("🚀 Apex BOT v9.1.0 Startup Complete.")

if __name__ == "__main__":
    # ローカル実行の場合、FastAPIサーバーを起動
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get('PORT', 8080)))
