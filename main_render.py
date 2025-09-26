# ====================================================================================
# Apex BOT v6.0 - Render Cron Job対応版 (main_render.py)
# ====================================================================================

# 1. ライブラリのインポート
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
# Colab環境ではないため、nest_asyncioやIPython.displayは不要

# ====================================================================================
#                                    CONFIG
# ====================================================================================

# 環境変数から設定を読み込む (Renderの推奨方法)
JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'YOUR_COINGLASS_API_KEY')

# --- 動作設定 ---
# Cron Jobで実行されるため、メインループ間隔は不要

# --- APIエンドポイント ---
COINGLASS_API_HEADERS = {'accept': 'application/json', 'coinglass-api-key': COINGLASS_API_KEY}

# ====================================================================================
#                               CCXT CLIENTS & CORE FUNCTIONS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 

def initialize_ccxt_client():
    """BOT実行時にCCXTクライアントを初期化する"""
    global CCXT_CLIENT
    # プロキシはRender環境で設定されるため、コードからは削除
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

async def fetch_top_symbols_binance_or_default(limit: int = 30) -> Tuple[List[str], str]:
    """
    Binance Futuresから出来高TOP30を取得する。エラーを捕捉し、静的リストにフォールバックする。
    """
    logging.info("出来高TOP30銘柄の取得試行を開始...")
    
    try:
        if CCXT_CLIENT is None: 
            raise Exception("CCXTクライアントが初期化されていません。")

        tickers = await CCXT_CLIENT.fetch_tickers()
        
        swap_markets = [
             ticker for ticker in tickers.values() 
             if 'USDT' in ticker['symbol'] and '/USDT' in ticker['symbol']
        ]

        sorted_markets = sorted(
            swap_markets, 
            key=lambda x: x.get('quoteVolume', 0) if x.get('quoteVolume') is not None else 0,
            reverse=True
        )
        
        top_symbols = []
        stablecoins = ["USDT", "USDC", "DAI", "TUSD", "FDUSD"]
        for market in sorted_markets:
            base_currency = market['symbol'].split('/')[0]
            if base_currency not in stablecoins and base_currency not in top_symbols:
                top_symbols.append(base_currency)
            if len(top_symbols) >= limit:
                break
                
        if len(top_symbols) < limit / 2: 
             raise Exception(f"出来高リストが空でした (取得数 {len(top_symbols)}個)。")
                
        logging.info(f"✅ {CCXT_CLIENT_NAME} から {len(top_symbols)} 銘柄を取得成功。")
        return top_symbols, CCXT_CLIENT_NAME
            
    except Exception as e:
        logging.warning(f"❌ 出来高ランキング取得に失敗: {e}。静的リストに切り替えます。")
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
        change = prices.diff(10).abs()
        volatility = prices.diff().abs().rolling(window=10).sum().replace(0, 1e-9)
        er = change / volatility
        sc = (er * (2 / (2 + 1) - 2 / (30 + 1)) + 2 / (30 + 1)) ** 2
        kama = pd.Series(np.nan, index=prices.index)
        if len(prices) > 10:
             kama.iloc[10] = prices.iloc[10]
             for i in range(11, len(prices)):
                kama.iloc[i] = kama.iloc[i-1] + sc.iloc[i] * (prices.iloc[i] - kama.iloc[i-1])

        if kama.iloc[-1] > kama.iloc[-2] and prices.iloc[-1] > kama.iloc[-1]: # 簡易判定
             return "リスクオン (株高)"
        if kama.iloc[-1] < kama.iloc[-2] and prices.iloc[-1] < kama.iloc[-1]:
             return "リスクオフ (株安)"
        return "中立"
    except Exception:
        return "不明"

# --- ANALYSIS ENGINE (変更なし) ---
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
    # ... (通知ロジックの後半は変更なし)
    
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


async def run_analysis_cycle(symbols_to_monitor: List[str]):
    """メイン分析サイクル (1回実行)"""
    notified_symbols = {}
    macro_context = get_tradfi_macro_context()
    
    # 分析の実行
    tasks = [analyze_symbol_and_notify(sym, macro_context, notified_symbols) for sym in symbols_to_monitor]
    await asyncio.gather(*tasks)


async def main():
    """
    Render Cron Job のエントリポイント
    BOTの起動、銘柄取得、分析実行を全て1回の実行で完了する
    """
    
    # 外部プロキシリストの取得 (一度だけ)
    global PROXY_LIST
    if not PROXY_LIST:
        PROXY_LIST = get_proxy_list_from_web()
    
    # CCXTクライアントの初期化 (最初の試行はプロキシなし)
    initialize_ccxt_client(get_proxy_options(None))
    
    logging.info("==================================================")
    logging.info(f"Apex BOT v6.0 Render Cron 起動: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 1. 銘柄取得 (プロキシリトライロジックを使用)
    symbols_to_monitor, source_exchange = await fetch_top_symbols_binance_or_default(30)
    
    logging.info(f"銘柄選定元: {source_exchange}")
    logging.info(f"監視対象 (TOP30): {', '.join(symbols_to_monitor[:5])} ...")
    logging.info("--------------------------------------------------")
    
    # 2. 分析実行
    await run_analysis_cycle(symbols_to_monitor)
    
    # 3. クローズ処理
    logging.info("CCXTクライアントを安全にクローズしています...")
    if CCXT_CLIENT: 
        await CCXT_CLIENT.close()
    
    logging.info(f"分析サイクル完了。Renderプロセスを終了します。")
    logging.info("==================================================")


# ====================================================================================
#                                    実行
# ====================================================================================
# Render環境では、メインループを直接実行
if __name__ == "__main__":
    try:
        # Colab環境でのエラー回避ロジックをRender用に簡素化
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Renderプロセスで致命的なエラーが発生し終了: {e}")
