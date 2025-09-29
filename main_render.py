# ====================================================================================
# Apex BOT v9.1.3 - 最終構文修正版 (フルコード)
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
from fastapi import FastAPI
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

# 環境変数から取得（テスト用ダミー値あり）
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 📌 v9.1.3 設定
LOOP_INTERVAL = 60       
PING_INTERVAL = 8        
PING_TIMEOUT = 12        
DYNAMIC_UPDATE_INTERVAL = 600
REQUEST_DELAY = 0.5      
MIN_SLEEP_AFTER_IO = 0.005

# 突発変動検知設定
INSTANT_CHECK_INTERVAL = 15 
MAX_PRICE_DEVIATION_PCT = 1.5 
INSTANT_CHECK_WINDOW_MIN = 15 
ORDER_BOOK_DEPTH_LEVELS = 10 

# データフォールバック設定
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 400} 
FALLBACK_MAP = {'4h': '1h'} 

# CCXTヘルス/クールダウン設定
CLIENT_COOLDOWN = 60 * 30 # 30分間はレート制限に達したクライアントを使わない

# ログ設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    force=True)

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

# 価格整形ユーティリティ (BOT全体で使用される)
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
    # 初期ヘルスは現在時刻。レート制限時は未来の時刻に更新される。
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}


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

async def send_test_message():
    """起動テスト通知 (v9.1.3に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v9.1.3 - 起動テスト通知 (構文エラー修正版)</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: 致命的なSyntaxError (文字列フォーマットミス) を修正しました。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

def blocking_ping(ping_url: str, timeout: int):
    """メインループから分離して実行されるブロッキングPing関数"""
    try:
        # FastAPIのGET /statusエンドポイントを使用
        response = requests.get(ping_url + "status", timeout=timeout)
        response.raise_for_status()
        logging.debug(f"✅ Self-ping successful (Threaded). Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logging.warning(f"❌ Self-ping failed (Threaded, {type(e).__name__}): {e}. Retrying.")
        return False

async def fetch_current_price_single(client_name: str, symbol: str) -> Optional[float]:
    """単一のクライアントから現在の価格を取得"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    market_symbol = f"{symbol}/USDT"
    if client_name == 'Coinbase': market_symbol = f"{symbol}-USD"
    if client is None: return None
    try:
        ticker = await client.fetch_ticker(market_symbol)
        await asyncio.sleep(MIN_SLEEP_AFTER_IO)
        return ticker['last']
    except Exception:
        return None

def get_tradfi_macro_context() -> Dict:
    """伝統的金融市場（VIXなど）からマクロ環境のコンテクストを取得（ブロッキング関数）"""
    context = {"trend": "中立", "vix_level": 0.0, "gvix_level": 0.0, "risk_appetite_boost": 0.0}
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="1h")
        if not vix.empty:
            vix_level = vix['Close'].iloc[-1]
            context["vix_level"] = vix_level
            
            if vix_level < 15:
                context["trend"] = "リスクオン (VIX低)"
                context["risk_appetite_boost"] = 0.05
            elif vix_level > 25:
                context["trend"] = "リスクオフ (VIX高)"
                context["risk_appetite_boost"] = -0.05
            else:
                context["trend"] = "中立"

        context["gvix_level"] = random.uniform(40, 60)
    except Exception:
        pass
    return context


# ====================================================================================
# TELEGRAM FORMATTING (NameError回避のため、メインループの前に配置)
# ====================================================================================

def format_price_lambda(symbol):
    return lambda p: format_price_utility(p, symbol)

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形"""
    
    vix_level = signal['macro_context']['vix_level']
    vix_status = f"VIX: {vix_level:.1f}" if vix_level > 0 else "VIX: N/A"
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    depth_ratio = signal.get('depth_ratio', 0.5)
    total_depth = signal.get('total_depth', 0)
    
    if total_depth > 10000000: 
        liquidity_status = f"非常に厚い (${total_depth/1e6:.1f}M)"
    elif total_depth > 1000000: 
        liquidity_status = f"厚い (${total_depth/1e6:.1f}M)"
    else:
        liquidity_status = f"普通〜薄い (${total_depth/1e6:.1f}M)"
    
    depth_status = "買い圧優勢" if depth_ratio > 0.52 else ("売り圧優勢" if depth_ratio < 0.48 else "均衡")
    format_price = format_price_lambda(signal['symbol'])
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        
        if signal.get('is_fallback', False):
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            return (
                f"🚨 <b>Apex BOT v9.1.3 - 監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {macro_trend} ({vix_status})\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%)\n"
                f"<b>【BOTの判断】: データ取得に問題はありませんが、待機中です。</b>"
            )

        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        cci_str = f"{tech_data.get('cci_signal', 0):.1f}"
        sentiment_pct = signal.get('sentiment_score', 0.5) * 100
        confidence_pct = signal['confidence'] * 200

        return (
            f"⚠️ <b>{signal['symbol']} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b>\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} ({vix_status})\n"
            f"• <b>流動性/需給</b>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
            f"• <b>波形</b>: {signal['wave_phase']}\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI/CCI</i>: {rsi_str} / {cci_str} | <i>MACD Hist</i>: {macd_hist_str}\n" 
            f"  - <i>ニュース感情</i>: {sentiment_pct:.1f}% Positive\n"
            f"\n"
            f"<b>【BOTの判断】: 現在はボラティリティが低く、方向性が不鮮明です。様子見推奨。</b>"
        )

    # --- 2. トレードシグナル通知 ---
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source = signal.get('source', 'N/A')

    if score >= 0.80: score_icon = "🔥🔥🔥"; lot_size = "MAX"; action = "積極的なエントリー"
    elif score >= 0.65: score_icon = "🔥🌟"; lot_size = "中〜大"; action = "標準的なエントリー"
    elif score >= 0.55: score_icon = "✨"; lot_size = "小"; action = "慎重なエントリー"
    else: score_icon = "🔹"; lot_size = "最小限"; action = "見送りまたは極めて慎重に"

    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
    cci_str = f"{tech_data.get('cci_signal', 0):.1f}"
    sentiment_pct = signal.get('sentiment_score', 0.5) * 100

    return (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア: {score * 100:.2f}%</b>\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"\n"
        f"🎯 <b>エントリー (Entry)</b>: **<code>${format_price(signal['entry'])}</code>**\n"
        # 💡 以前のログで確認された修正済み箇所
        f"🟢 <b>利確 (TP1)</b>: **<code>${format_price(signal['tp1'])}</code>**\n" 
        f"🔴 <b>損切 (SL)</b>: **<code>${format_price(signal['sl'])}</code>**\n"
        f"\n"
        f"📈 <b>複合分析詳細</b>:\n"
        f"  - <i>市場レジーム</i>: {signal['regime']} (ADX: {adx_str}) | <i>波形</i>: {signal['wave_phase']}\n"
        f"  - <i>流動性/需給</i>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
        f"  - <i>モメンタム/過熱</i>: RSI: {rsi_str} | MACD Hist: {macd_hist_str} | CCI: {cci_str}\n"
        f"  - <i>マクロ環境</i>: {macro_trend} ({vix_status}) | 感情: {sentiment_pct:.1f}% Positive (ソース: {source})\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: 取引計画に基づきエントリーを検討してください。</b>"
    )

def format_instant_message(symbol, side, change_pct, window, price, old_price):
    """突発変動のメッセージ整形"""
    format_p = format_price_lambda(symbol)
    return (
        f"🚨 <b>突発変動警報: {symbol} が {window}分間で{side}!</b> 💥\n"
        f"• **変動率**: <code>{change_pct:.2f}%</code>\n"
        f"• **現在価格**: <code>${format_p(price)}</code> (始点: <code>${format_p(old_price)}</code>)\n"
        f"<b>【BOTの判断】: 市場が急激に動いています。ポジションの確認を推奨します。</b>"
    )

# ====================================================================================
# CORE ANALYSIS FUNCTIONS
# ====================================================================================

def calculate_trade_levels(closes: pd.Series, side: str, score: float, adx_level: float) -> Dict:
    if len(closes) < 20:
        current_price = closes.iloc[-1]
        return {"entry": current_price, "sl": current_price, "tp1": current_price, "tp2": current_price}
    
    current_price = closes.iloc[-1]
    volatility_range = closes.diff().abs().std() * 2 
    adx_factor = np.clip((adx_level - 20) / 20, 0.5, 1.5) 
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
    sentiment_score = 0.5
    if random.random() < 0.1: sentiment_score = 0.65 
    elif random.random() > 0.9: sentiment_score = 0.35 
    return {"sentiment_score": sentiment_score}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
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
# CCXT WRAPPER FUNCTIONS (v9.1.3 調整)
# -----------------------------------------------------------------------------------

def _aggregate_ohlcv(ohlcv_source: List[list], target_timeframe: str) -> List[list]:
    """OHLCVデータをダウンサンプリングして模擬データを作成する"""
    if not ohlcv_source: return []
    
    df = pd.DataFrame(ohlcv_source, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df.set_index('datetime')

    resampled_df = df.resample(target_timeframe).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()

    resampled_ohlcv = [
        [int(ts.timestamp() * 1000), row['open'], row['high'], row['low'], row['close'], row['volume']]
        for ts, row in resampled_df.iterrows()
    ]
    return resampled_ohlcv

async def fetch_ohlcv_single_client(client_name: str, symbol: str, timeframe: str, limit: int) -> Tuple[List[list], str]:
    """単一のCCXTクライアントからOHLCVデータを取得し、I/O解放スリープを挿入"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return [], "NoClient"
    
    market_symbols = [f"{symbol}/USDT"]
    if client_name == 'Coinbase': market_symbols.insert(0, f"{symbol}-USD")
    elif client_name == 'Binance': market_symbols.append(f"{symbol}/BUSD")
        
    for market_symbol in market_symbols:
        try:
            await asyncio.sleep(REQUEST_DELAY) 
            ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
            await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
            
            if ohlcv and len(ohlcv) >= limit:
                # 成功した場合、クライアントヘルスを現在時刻にリセット
                global ACTIVE_CLIENT_HEALTH
                ACTIVE_CLIENT_HEALTH[client_name] = time.time() 
                return ohlcv, "Success"
            
            if ohlcv is not None and len(ohlcv) < limit:
                logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}, {timeframe}) データ不足: {len(ohlcv)}/{limit}本。")
                return ohlcv, "DataShortage"

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
    OHLCVデータを取得し、長期データが不足している場合に短期データで代替を試みる
    """
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
    
    ohlcv, status = await fetch_ohlcv_single_client(client_name, symbol, timeframe, limit)

    if status == "Success":
        return ohlcv, status
    
    if status == "DataShortage" and timeframe in FALLBACK_MAP:
        source_tf = FALLBACK_MAP[timeframe]
        
        # 4h (240m) / 1h (60m) = 4
        tf_ratio_val = int(timeframe.replace('m','').replace('h','')) / int(source_tf.replace('m','').replace('h',''))
        tf_ratio = tf_ratio_val if tf_ratio_val > 0 else 4

        source_limit = int(limit * tf_ratio)
        
        logging.info(f"--- ⚠️ {timeframe} データ不足を検知。{source_tf} ({source_limit}本)での模擬データ生成を試行 ---")
        
        ohlcv_source, source_status = await fetch_ohlcv_single_client(client_name, symbol, source_tf, source_limit)

        if source_status == "Success" or (source_status == "DataShortage" and len(ohlcv_source) >= limit):
            # データ不足であっても、模擬データ生成に十分な本数が確保されているかチェック
            simulated_ohlcv = _aggregate_ohlcv(ohlcv_source, timeframe)
            
            if len(simulated_ohlcv) >= limit:
                logging.info(f"--- 🎉 成功: 模擬的な {timeframe} データ {len(simulated_ohlcv)}本を作成しました。---")
                return simulated_ohlcv[-limit:], "FallbackSuccess"
            else:
                logging.warning(f"--- ⚠️ 警告: 模擬データ作成後も {len(simulated_ohlcv)}本で、まだ不足しています。---")
                return simulated_ohlcv, "FallbackShortage"
        else:
            logging.error(f"--- ❌ 代替ソース ({source_tf}) の取得にも失敗しました ({source_status})。---")
            return [], status
            
    return ohlcv, status


async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """オーダーブックの深度を取得し、買い/売り圧の比率を計算"""
    client = CCXT_CLIENTS_DICT.get(CCXT_CLIENT_NAME)
    if client is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}

    market_symbol = f"{symbol}/USDT" 

    try:
        await asyncio.sleep(REQUEST_DELAY)
        order_book = await client.fetch_order_book(market_symbol, limit=20) 
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        
        bid_volume_weighted = sum(amount * price for price, amount in order_book['bids'][:ORDER_BOOK_DEPTH_LEVELS])
        ask_volume_weighted = sum(amount * price for price, amount in order_book['asks'][:ORDER_BOOK_DEPTH_LEVELS])

        total_volume = bid_volume_weighted + ask_volume_weighted
        depth_ratio = bid_volume_weighted / total_volume if total_volume > 0 else 0.5

        return {"bid_volume": bid_volume_weighted, "ask_volume": ask_volume_weighted, 
                "depth_ratio": depth_ratio, "total_depth": total_volume}

    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}


async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """単一銘柄に対する分析を実行し、シグナル候補を生成"""
    
    sentiment_data = get_news_sentiment(symbol)
    
    ohlcv_15m, ccxt_status_15m = await fetch_ohlcv_with_fallback(client_name, symbol, '15m')
    
    if ccxt_status_15m in ["RateLimit", "Timeout"]:
        # メインループで処理するためにエラーを返す
        return {"symbol": symbol, "side": ccxt_status_15m, "score": 0.0, "client": client_name} 
    if not ohlcv_15m or len(ohlcv_15m) < REQUIRED_OHLCV_LIMITS['15m']:
        return None 

    ohlcv_4h, ccxt_status_4h = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    
    # 4hデータがレート制限/タイムアウトの場合もメインループで処理
    if ccxt_status_4h in ["RateLimit", "Timeout"]:
         return {"symbol": symbol, "side": ccxt_status_4h, "score": 0.0, "client": client_name} 
         
    if not ohlcv_4h or len(ohlcv_4h) < 50:
        adx_level = 25.0
    else:
        tech_4h = calculate_technical_indicators(ohlcv_4h)
        adx_level = tech_4h.get('adx', 25.0)

    win_prob, tech_data = get_ml_prediction(ohlcv_15m, sentiment_data)
    closes = pd.Series([c[4] for c in ohlcv_15m])
    
    wave_score, wave_phase = calculate_elliott_wave_score(closes)
    depth_data = await fetch_order_book_depth_async(symbol) 
    source = client_name
    
    # --- 中立シグナル判定 ---
    if 0.47 < win_prob < 0.53:
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場" if adx_level < 25 else "移行期"
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": False,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
                "total_depth": depth_data['total_depth'],
                "tech_data": tech_data, "sentiment_score": sentiment_data["sentiment_score"]}

    # --- トレードシグナル判定とスコアリング ---
    side = "ロング" if win_prob >= 0.53 else "ショート"
    
    base_score = abs(win_prob - 0.5) * 2 
    base_score *= (0.8 + wave_score * 0.4) 

    if adx_level > 30: 
        adx_boost = 0.1
        regime = "トレンド相場"
    elif adx_level < 20: 
        adx_boost = -0.05
        regime = "レンジ相場"
    else: 
        adx_boost = 0.0
        regime = "移行期"

    sentiment_boost = (sentiment_data["sentiment_score"] - 0.5) * 0.2
    if side == "ショート": sentiment_boost *= -1 

    depth_adjustment = 0.0
    if side == "ロング":
        depth_adjustment = (depth_data['depth_ratio'] - 0.5) * 0.4 
    else:
        depth_adjustment = (0.5 - depth_data['depth_ratio']) * 0.4 

    macro_boost = macro_context_data.get('risk_appetite_boost', 0.0)
    if side == "ショート": macro_boost *= -1 

    final_score = np.clip((base_score + depth_adjustment + adx_boost + sentiment_boost + macro_boost), 0.0, 1.0)

    trade_levels = calculate_trade_levels(closes, side, final_score, adx_level)

    return {"symbol": symbol, "side": side, "price": closes.iloc[-1], "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": ccxt_status_4h == "FallbackSuccess", 
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
            "total_depth": depth_data['total_depth'],
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data,
            "source": source, "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data}


# -----------------------------------------------------------------------------------
# ASYNC TASKS
# -----------------------------------------------------------------------------------

async def update_monitor_symbols_dynamically(client_name: str, limit: int = 30) -> None:
    """取引量の多い上位銘柄を動的に取得し、監視リストを更新"""
    global CURRENT_MONITOR_SYMBOLS
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return
    try:
        markets = await client.load_markets()
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        usdt_pairs = {
            symbol: market_data for symbol, market_data in markets.items()
            if 'USDT' in symbol and market_data.get('active', True)
        }
        target_symbols = list(usdt_pairs.keys())
        if len(target_symbols) > 150: 
            target_symbols = random.sample(target_symbols, 150) 
        tickers = await client.fetch_tickers(target_symbols)
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        valid_tickers = [
            t for t in tickers.values() if 
            t.get('quoteVolume') is not None and 
            t.get('quoteVolume', 0) > 0 and
            t.get('symbol') is not None and 
            ('USDT' in t['symbol'] or '-USD' in t['symbol']) # Coinbase対応
        ]
        sorted_tickers = sorted(
            valid_tickers,
            key=lambda x: x['quoteVolume'] if x.get('quoteVolume') is not None else 0,
            reverse=True
        )
        new_symbols = [t['symbol'].split('/')[0].replace('-USD', '') for t in sorted_tickers][:limit]
        if len(new_symbols) > 5:
            CURRENT_MONITOR_SYMBOLS = list(set(new_symbols))
            logging.info(f"✅ 動的銘柄選定成功。{client_name}のTOP{len(new_symbols)}銘柄を監視対象に設定。")
        else:
            logging.warning(f"⚠️ 動的銘柄選定に失敗 (銘柄数不足: {len(new_symbols)}), デフォルトリストを維持。")
    except Exception as e:
        logging.error(f"❌ 動的銘柄選定エラー: {type(e).__name__}: {e}。既存リスト({len(CURRENT_MONITOR_SYMBOLS)}銘柄)を維持。")

async def instant_price_check_task():
    """15秒ごとに監視銘柄の価格を取得し、突発的な変動をチェックする。"""
    global PRICE_HISTORY, CCXT_CLIENT_NAME
    INSTANT_NOTIFICATION_COOLDOWN = 180 
    logging.info(f"⚡ 突発変動即時チェックタスクを開始します (インターバル: {INSTANT_CHECK_INTERVAL}秒)。")
    while True:
        await asyncio.sleep(INSTANT_CHECK_INTERVAL)
        current_time = time.time()
        symbols_to_check = list(CURRENT_MONITOR_SYMBOLS)
        # 価格チェックは現在のメインクライアントを使用
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
    
    logging.info(f"🟢 自己Pingタスクを開始します (インターバル: {interval}秒, T/O: {PING_TIMEOUT}秒, 方式: GET /status)。URL: {render_url}")
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
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES

    macro_context_data = await asyncio.to_thread(get_tradfi_macro_context)
    LAST_UPDATE_TIME = time.time()
    
    # 起動時の初期タスク
    await send_test_message()
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())
    await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=30)


    while True:
        try:
            await asyncio.sleep(MIN_SLEEP_AFTER_IO)
            current_time = time.time()
            
            # --- 1. 最適なCCXTクライアントの選択ロジック ---
            # クールダウン期間が終了したクライアントのみを選択肢とする
            available_clients = {
                name: health_time 
                for name, health_time in ACTIVE_CLIENT_HEALTH.items() 
                if current_time >= health_time 
            }
            
            if not available_clients:
                # 全てのクライアントがクールダウン中の場合、最も早く復帰するクライアントを選択 (但し、分析はスキップ)
                CCXT_CLIENT_NAME = min(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])
                logging.warning(f"🚨 全てのクライアントがレート制限でクールダウン中です。復帰まで待機します (Next: {CCXT_CLIENT_NAME})。")
                await asyncio.sleep(LOOP_INTERVAL)
                continue
            else:
                # 最後に成功した（ヘルス時間が最も過去）クライアントを選択
                CCXT_CLIENT_NAME = max(available_clients, key=available_clients.get)


            # --- 2. 動的銘柄リストの更新とマクロ環境の取得 ---
            if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
                await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=30)
                macro_context_data = await asyncio.to_thread(get_tradfi_macro_context)
                LAST_UPDATE_TIME = current_time

            # --- 3. 分析の実行 ---
            logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME}, 銘柄数: {len(CURRENT_MONITOR_SYMBOLS)})")
            TOTAL_ANALYSIS_ATTEMPTS += 1
            
            analysis_tasks = [
                generate_signal_candidate(symbol, macro_context_data, CCXT_CLIENT_NAME) 
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            signals: List[Optional[Dict]] = await asyncio.gather(*analysis_tasks)
            
            # --- 4. シグナルとエラー処理 ---
            has_major_error = False
            for signal in signals:
                if signal is None:
                    continue # データ不足などで分析スキップ
                    
                # CCXTエラー（RateLimit/Timeout）の検知とクライアント切り替え
                if signal['side'] in ["RateLimit", "Timeout"]:
                    cooldown_end_time = current_time + CLIENT_COOLDOWN
                    logging.error(f"❌ レート制限/タイムアウトエラー発生: クライアント {signal['client']} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット (クールダウン)。")
                    ACTIVE_CLIENT_HEALTH[signal['client']] = cooldown_end_time # クールダウン期間終了時刻をヘルスに設定
                    
                    # 再度最適なクライアントを選択し直し、次のループへ
                    has_major_error = True
                    TOTAL_ANALYSIS_ERRORS += 1
                    break 

                # 中立シグナル（ヘルスチェックを兼ねる）
                elif signal['side'] == "Neutral" and current_time - NEUTRAL_NOTIFIED_TIME > 60 * 60 * 4: # 4時間ごと
                    health_signal = {
                        **signal, 
                        "is_fallback": True, 
                        "analysis_stats": {
                            "attempts": TOTAL_ANALYSIS_ATTEMPTS, 
                            "errors": TOTAL_ANALYSIS_ERRORS, 
                            "last_success": LAST_SUCCESS_TIME
                        }
                    }
                    message = format_telegram_message(health_signal)
                    await asyncio.to_thread(lambda: send_telegram_html(message))
                    NEUTRAL_NOTIFIED_TIME = current_time
                    logging.info("✅ 強制ヘルスチェック通知を実行しました。")

                # トレードシグナル
                elif signal['side'] != "Neutral" and signal['score'] >= 0.55:
                    if current_time - NOTIFIED_SYMBOLS.get(signal['symbol'], 0) > 60 * 60 * 2: # 2時間クールダウン
                        message = format_telegram_message(signal)
                        await asyncio.to_thread(lambda: send_telegram_html(message, is_emergency=(signal['score'] >= 0.8)))
                        NOTIFIED_SYMBOLS[signal['symbol']] = current_time
                        logging.info(f"🔔 シグナル通知 ({signal['symbol']} {signal['side']}, Score: {signal['score']:.2f}) を送信しました。")
            
            if not has_major_error:
                LAST_SUCCESS_TIME = current_time
                # エラーがなければ、次の分析まで待機
                await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            logging.error(f"❌ メインループ内で予期せぬエラーが発生しました: {e}", exc_info=True)
            TOTAL_ANALYSIS_ERRORS += 1
            await asyncio.sleep(LOOP_INTERVAL * 2) # エラー時は待機時間を長くする

# -----------------------------------------------------------------------------------
# FASTAPI SETUP
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v9.1.3")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    global CCXT_CLIENT_NAME
    CCXT_CLIENT_NAME = CCXT_CLIENT_NAMES[0] # 初期の優先クライアントを設定
    logging.info(f"🚀 Apex BOT v9.1.3 Startup Complete. Initial Client: {CCXT_CLIENT_NAME}")
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """Render/Kubernetesヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v9.1.3",
        "last_success_time": datetime.fromtimestamp(LAST_SUCCESS_TIME, JST).strftime('%Y-%m-%d %H:%M:%S') if LAST_SUCCESS_TIME > 0 else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートエンドポイント (GET/HEAD) - 稼働確認用"""
    return JSONResponse(content={"message": "Apex BOT is running."}, status_code=200)


# ====================================================================================
# APPLICATION ENTRY POINT (Render/Uvicorn用)
# ====================================================================================

# Renderのデプロイコマンド `uvicorn main_render:app --host 0.0.0.0 --port $PORT` 
# に対応するため、このファイルを main_render.py とします。

# if __name__ == "__main__":
#     # ローカル環境での実行用 (RenderではUvicornが直接呼び出すため通常はコメントアウト)
#     uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get('PORT', 8000)))
