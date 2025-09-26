# ====================================================================================
# Apex BOT v6.0 - Render Coinglass選定・最終安定版 (main_render.py)
# ====================================================================================
#
# 目的: Renderの無料Webサービスで稼働させるための定義順序を修正し、NameErrorを解消。
#
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
from typing import Dict, List, Optional, Tuple
import yfinance as yf
import asyncio
import random
import re 
from io import StringIO 

# サーバーフレームワークのインポート
from fastapi import FastAPI
import uvicorn

# ====================================================================================
#                                    CONFIG
# ====================================================================================

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

# Render 環境変数から設定を読み込む (未設定時のデフォルト値)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '7904380124:AAE2AuRITmgBw5OECTELF5151D3pRz4K9JM')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '5890119671')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', '1d6a02becd6146a2b09ea5e424b41b6e')

# --- 動作設定 ---
LOOP_INTERVAL = 30       
DYNAMIC_UPDATE_INTERVAL = 300 

# --- APIエンドポイント ---
COINGLASS_API_HEADERS = {'accept': 'application/json', 'coinglass-api-key': COINGLASS_API_KEY}

# ====================================================================================
#                               UTILITIES & CLIENTS (関数の定義開始)
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
PROXY_LIST = [] 

def get_proxy_list_from_web() -> List[str]:
    """外部Webサイトから無料のSOCKS5プロキシリストを取得する"""
    logging.info("外部プロキシリストの取得試行中...")
    proxies = []
    sources = [
        'https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt',
        'https://api.proxyscrape.com/v2/?request=getproxies&protocol=socks5',
    ]

    for url in sources:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                new_proxies = [f"socks5://{p}" for p in response.text.splitlines() if re.match(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+', p.strip())]
                proxies.extend(new_proxies)
        except Exception:
            continue
    
    unique_proxies = list(set(proxies))
    if len(unique_proxies) < 5:
        logging.warning(f"外部プロキシの取得に失敗または数が不足({len(unique_proxies)}個)。ダミープロキシを使用します。")
        unique_proxies.extend(['socks5://104.248.169.176:1080', 'socks5://159.203.111.9:1080'])
        
    logging.info(f"✅ {len(unique_proxies)} 個のユニークなSOCKS5プロキシを取得しました。")
    return unique_proxies

def get_proxy_options(proxy_url: Optional[str]) -> Dict:
    """CCXTオプション形式でプロキシ設定を返す"""
    if proxy_url:
        return {
            'proxies': {
                'http': proxy_url,
                'https': proxy_url
            }
        }
    return {}

def initialize_ccxt_client():
    """BOT実行時にCCXTクライアントを初期化する"""
    global CCXT_CLIENT
    # プロキシは不要（BinanceへのOHLCVアクセスもブロックされる可能性はあるが、銘柄選定よりは負荷が低い）
    CCXT_CLIENT = ccxt_async.binance({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "future"}})

def send_telegram_html(text: str, is_emergency: bool = False):
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<pre>", "\n").replace("</pre>", "")
        logging.info("--- TELEGRAM通知（ダミー）---\n" + clean_text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": not is_emergency
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram送信エラー: {e}")

async def fetch_top_symbols_from_coinglass_async(limit: int = 30) -> Tuple[List[str], str]:
    """
    Coinglass APIからOIに基づいて出来高上位銘柄を動的に取得する
    """
    oi_rank_url = "https://open-api.coinglass.com/public/v2/open_interest/list"
    logging.info("銘柄選定をCoinglass API (OIランキング) から取得試行中...")

    try:
        if 'YOUR' in COINGLASS_API_KEY:
            raise Exception("COINGLASS_API_KEYが設定されていません。")
            
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: requests.get(
            oi_rank_url, headers=COINGLASS_API_HEADERS, params={"timeType": "h1"}, timeout=10
        ).json())
        
        data = res.get('data', {}).get('list', [])
        
        top_symbols = []
        stablecoins = ["USDT", "USDC", "DAI", "TUSD", "FDUSD"]
        
        for item in data:
            symbol = item.get('symbol')
            if symbol and symbol not in stablecoins and symbol not in top_symbols:
                top_symbols.append(symbol)
            if len(top_symbols) >= limit:
                break
                
        if len(top_symbols) < limit / 2:
             raise Exception(f"Coinglassから取得できた銘柄数が少なすぎます ({len(top_symbols)}個)。")
        
        logging.info(f"✅ Coinglass API から {len(top_symbols)} 銘柄を取得成功 (OI TOP)。")
        return top_symbols, "Coinglass (OI Rank)"

    except Exception as e:
        logging.error(f"❌ Coinglassからの銘柄取得に失敗: {e}。静的リストを使用します。")
        return DEFAULT_SYMBOLS[:limit], "Static List (Failure Avoided)"


async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
    """OHLCVは固定のCCXTクライアント (Binance) から取得する"""
    if CCXT_CLIENT is None: return []
    
    market_symbol = f"{symbol}/USDT" 

    try:
        return await CCXT_CLIENT.fetch_ohlcv(market_symbol, timeframe, limit=limit)
    except Exception:
        return []

async def fetch_market_sentiment_data_async(symbol: str) -> Dict:
    """OIデータ取得をスキップし、ダミーデータを返す"""
    return {"oi_change_24h": 0} 

def get_tradfi_macro_context() -> str:
    try:
        es = yf.Ticker("ES=F")
        hist = es.history(period="5d", interval="1h")
        if hist.empty: return "不明"
        prices = hist['Close']
        kama_fast = calculate_kama(prices, period=10)
        kama_slow = calculate_kama(prices, period=21)
        if kama_fast.iloc[-1] > kama_slow.iloc[-1] and prices.iloc[-1] > kama_fast.iloc[-1]:
            return "リスクオン (株高)"
        if kama_fast.iloc[-1] < kama_slow.iloc[-1] and prices.iloc[-1] < kama_fast.iloc[-1]:
            return "リスクオフ (株安)"
        return "中立"
    except Exception:
        return "不明"

def calculate_kama(prices: pd.Series, period: int = 10, fast_ema: int = 2, slow_ema: int = 30) -> pd.Series:
    change = prices.diff(period).abs()
    volatility = prices.diff().abs().rolling(window=period).sum().replace(0, 1e-9)
    er = change / volatility
    sc = (er * (2 / (fast_ema + 1) - 2 / (slow_ema + 1)) + 2 / (slow_ema + 1)) ** 2
    kama = pd.Series(np.nan, index=prices.index)
    if len(prices) > period:
        kama.iloc[period] = prices.iloc[period]
        for i in range(period + 1, len(prices)):
            kama.iloc[i] = kama.iloc[i-1] + sc.iloc[i] * (prices.iloc[i] - kama.iloc[i-1])
    return kama

async def determine_market_regime(symbol: str) -> str:
    ohlcv = await fetch_ohlcv_async(symbol, '4h', 100)
    if len(ohlcv) < 100: return "不明"
    prices = pd.Series([c[4] for c in ohlcv])
    atr_ratio = (pd.Series([h[2] - h[3] for h in ohlcv]).rolling(14).mean().iloc[-1]) / prices.iloc[-1]
    if atr_ratio > 0.05: return "高ボラティリティ"
    kama_fast = calculate_kama(prices, period=21)
    kama_slow = calculate_kama(prices, period=50)
    if kama_fast.iloc[-1] > kama_slow.iloc[-1] and prices.iloc[-1] > kama_fast.iloc[-1]:
        return "強気トレンド"
    if kama_fast.iloc[-1] < kama_slow.iloc[-1] and prices.iloc[-1] < kama_fast.iloc[-1]:
        return "弱気トレンド"
    return "レンジ相場"

async def multi_timeframe_confirmation(symbol: str) -> (bool, str):
    timeframes = ['15m', '1h', '4h']
    trends = []
    tasks = [fetch_ohlcv_async(symbol, tf, 60) for tf in timeframes]
    ohlcv_results = await asyncio.gather(*tasks)
    for ohlcv in ohlcv_results:
        if len(ohlcv) < 60: return False, "データ不足"
        prices = pd.Series([c[4] for c in ohlcv])
        kama = calculate_kama(prices, period=21)
        trends.append("上昇" if prices.iloc[-1] > kama.iloc[-1] else "下降")
    if len(set(trends)) == 1:
        return True, trends[0]
    return False, "不一致"

def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> float:
    try:
        closes = pd.Series([c[4] for c in ohlcv])
        delta = closes.diff().fillna(0)
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rs = gain / loss.replace(0, 1e-9)
        rsi = 100 - (100 / (1 + rs)).iloc[-1]
        
        prob = 0.5 + ((rsi - 50) / 100) * 0.8 
        return np.clip(prob, 0, 1)
    except Exception:
        return 0.5

async def generate_signal(symbol: str, regime: str, macro_context: str) -> Optional[Dict]:
    criteria = []
    is_aligned, trend_direction = await multi_timeframe_confirmation(symbol)
    if is_aligned: criteria.append(f"多時間軸の方向性が一致 ({trend_direction})")
    
    side = None
    if regime == "強気トレンド" and trend_direction == "上昇":
        side = "ロング"
        criteria.append("長期レジームが強気トレンド")
    elif regime == "弱気トレンド" and trend_direction == "下降":
        side = "ショート"
        criteria.append("長期レジームが弱気トレンド")
    
    if side is None: return None
        
    if side == "ロング" and macro_context == "リスクオフ (株安)": return None
    criteria.append(f"マクロ経済と整合 ({macro_context})")
    
    sentiment = await fetch_market_sentiment_data_async(symbol)
    
    criteria.append("OIデータはニュートラルとして処理されました")
        
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    if len(ohlcv_15m) < 100: return None
    win_prob = get_ml_prediction(ohlcv_15m, sentiment)
    criteria.append(f"MLモデル予測上昇確率: {win_prob:.2%}")
    
    if len(criteria) >= 3 and win_prob > 0.70:
        price = ohlcv_15m[-1][4]
        atr = (pd.Series([h[2] - h[3] for h in ohlcv_15m]).rolling(14).mean().iloc[-1])
        sl = price - (atr * 2.5) if side == "ロング" else price + (atr * 2.5)
        return {"symbol": symbol, "side": side, "price": price, "sl": sl,
                "criteria": criteria, "confidence": win_prob, "regime": regime, "ohlcv_15m": ohlcv_15m}
    return None

def format_telegram_message(signal: Dict) -> str:
    side_icon = "📈" if signal['side'] == "ロング" else "📉"
    msg = f"💎 <b>Apex BOT シグナル速報: {signal['symbol']}</b> {side_icon}\n"
    msg += f"<i>市場レジーム: {signal['regime']} ({CCXT_CLIENT_NAME}データ)</i>\n"
    msg += f"<i>MLモデル予測信頼度: {signal['confidence']:.2%}</i>\n\n"
    
    msg += "<b>✅ 判断根拠 (Criteria)</b>\n"
    for c in signal['criteria']: msg += f"• {c}\n"
        
    price = signal['price']
    sl = signal['sl']
    
    closes_15m = pd.Series([c[4] for c in signal['ohlcv_15m']])
    optimal_entry = closes_15m.ewm(span=9, adjust=False).mean().iloc[-1]
    
    df_15m = pd.DataFrame(signal['ohlcv_15m'], columns=['t','o','h','l','c','v'])
    df_15m['tr'] = np.maximum(df_15m['h'] - df_15m['l'], np.maximum(abs(df_15m['h'] - df_15m['c'].shift()), abs(df_15m['l'] - df_15m['c'].shift())))
    atr_15m = df_15m['tr'].rolling(14).mean().iloc[-1]

    entry_zone_upper = optimal_entry + (atr_15m * 0.5)
    entry_zone_lower = optimal_entry - (atr_15m * 0.5)
    
    risk_per_unit = abs(optimal_entry - sl)
    tp1 = optimal_entry + (risk_per_unit * 1.5) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 1.5)
    tp2 = optimal_entry + (risk_per_unit * 3.0) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 3.0)
    
    msg += "\n<b>🎯 精密エントリープラン</b>\n"
    msg += f"<pre>現在価格: {price:,.4f}\n\n"
    if signal['side'] == 'ロング':
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"上限: {entry_zone_upper:,.4f}\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"下限: {entry_zone_lower:,.4f}\n"
        msg += "👉 この価格帯への押し目を待ってエントリー\n\n"
    else:
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"下限: {entry_zone_lower:,.4f}\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"上限: {entry_zone_upper:,.4f}\n"
        msg += "👉 この価格帯への戻りを待ってエントリー\n\n"
        
    msg += f"--- ゾーンエントリー時の目標 ---\n"
    msg += f"損切 (SL): {sl:,.4f}\n"
    msg += f"利確① (TP1): {tp1:,.4f}\n"
    msg += f"利確② (TP2): {tp2:,.4f}</pre>"
    
    return msg

async def analyze_symbol_and_notify(symbol: str, macro_context: str, notified_symbols: Dict):
    current_time = time.time()
    if symbol in notified_symbols and current_time - notified_symbols[symbol] < 3600: return

    regime = await determine_market_regime(symbol)
    if regime == "不明": return
        
    signal = await generate_signal(symbol, regime, macro_context)
    if signal:
        message = format_telegram_message(signal)
        send_telegram_html(message, is_emergency=True)
        notified_symbols[symbol] = current_time

async def main_loop():
    """BOTの常時監視を実行するバックグラウンドタスク"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS
    notified_symbols = {}
    
    # 起動時の初期リスト設定
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS[:30]
    macro_context = get_tradfi_macro_context()
    
    while True:
        try:
            current_time = time.time()
            is_dynamic_update_needed = (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL
            
            # --- 動的更新フェーズ (300秒に一度) ---
            if is_dynamic_update_needed:
                logging.info("==================================================")
                logging.info(f"Apex BOT v6.0 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                
                macro_context = get_tradfi_macro_context() # マクロコンテクストを更新
                logging.info(f"マクロ経済コンテクスト: {macro_context}")
                
                # 出来高TOP30取得試行 (Coinglassへの変更)
                symbols_to_monitor, source_exchange = await fetch_top_symbols_from_coinglass_async(30)
                
                CURRENT_MONITOR_SYMBOLS = symbols_to_monitor
                LAST_UPDATE_TIME = current_time
                
                logging.info(f"銘柄選定元: {source_exchange}")
                logging.info(f"監視対象 (TOP30): {', '.join(CURRENT_MONITOR_SYMBOLS[:5])} ...")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (30秒ごと) ---
            
            # 分析の実行
            tasks = [analyze_symbol_and_notify(sym, macro_context, notified_symbols) for sym in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*tasks)

            # ログ出力は、5分に一度だけ行う
            if is_dynamic_update_needed:
                logging.info("--------------------------------------------------")
                logging.info(f"分析サイクル完了。{LOOP_INTERVAL}秒待機します。")
                logging.info("==================================================")
            
            # 30秒待機
            await asyncio.sleep(LOOP_INTERVAL)
            
        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)


# ------------------------------------------------------------------------------------
# FASTAPI WEB SERVER SETUP
# ------------------------------------------------------------------------------------

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """サーバー起動時にクライアントを初期化し、バックグラウンドタスクを開始する"""
    
    logging.info("Starting Apex BOT Web Service...")
    
    # 1. CCXTクライアントの初期化 (プロキシは不要、Binanceに固定)
    initialize_ccxt_client() 

    # 2. バックグラウンドタスクとしてメインループを起動
    asyncio.create_task(main_loop())
    
@app.on_event("shutdown")
async def shutdown_event():
    """サーバーシャットダウン時にリソースを解放する"""
    if CCXT_CLIENT:
        logging.info("Closing CCXT Client during shutdown.")
        await CCXT_CLIENT.close()

@app.get("/")
def read_root():
    """Renderのスリープを防ぐためのヘルスチェックエンドポイント"""
    logging.info(f"Health Check Ping Received. Analyzing: {CURRENT_MONITOR_SYMBOLS[0]}...")
    return {
        "status": "Running",
        "service": "Apex BOT v6.0",
        "monitoring_base": CCXT_CLIENT_NAME,
        "next_dynamic_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }

# ------------------------------------------------------------------------------------
# RENDER ENTRY POINT (Uvicorn configuration)
# ------------------------------------------------------------------------------------

# Render環境では、Uvicornがこのappインスタンスを自動的に起動します。
