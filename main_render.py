# ====================================================================================
# Apex BOT v6.1 - 最高スコア銘柄の強制通知ロジック実装版 (main_render.py)
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
from fastapi import FastAPI
import uvicorn
from dotenv import load_dotenv
load_dotenv()

# ====================================================================================
#                                    CONFIG
# ====================================================================================

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

# 環境変数から設定を読み込む
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# --- 動作設定 ---
LOOP_INTERVAL = 30       
DYNAMIC_UPDATE_INTERVAL = 300 # 銘柄リスト更新間隔を300秒 (5分) に設定

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
NOTIFIED_SYMBOLS = {}

def initialize_ccxt_client():
    global CCXT_CLIENT
    CCXT_CLIENT = ccxt_async.binance({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "future"}})

async def send_test_message():
    test_text = (
        f"🤖 <b>Apex BOT v6.1 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"Render環境でのWebサービス起動に成功しました。\n"
        f"**最高スコア銘柄の強制通知モード**で稼働中です。"
    )
    
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

def send_telegram_html(text: str, is_emergency: bool = False):
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<pre>", "\n").replace("</pre>", "")
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。実際の通知は行われず、ログに出力されます。")
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

async def fetch_top_symbols_async(limit: int = 30) -> Tuple[List[str], str]:
    """CoinGecko APIから市場時価総額TOPの銘柄を取得し、Binanceのシンボルに変換する。"""
    coingecko_url = "https://api.coingecko.com/api/v3/coins/markets"
    
    try:
        loop = asyncio.get_event_loop()
        params = {'vs_currency': 'usd','order': 'market_cap_desc','per_page': limit * 2,'page': 1,'sparkline': 'false'}
        res = await loop.run_in_executor(None, lambda: requests.get(coingecko_url, params=params, timeout=10).json())
        
        if not isinstance(res, list) or not res: raise Exception("CoinGecko APIがリストを返しませんでした。")

        top_symbols = []
        for item in res:
            symbol = item.get('symbol', '').upper()
            if symbol not in ['USD', 'USDC', 'DAI', 'BUSD'] and len(symbol) <= 5: 
                top_symbols.append(symbol)
            if len(top_symbols) >= limit: break

        if len(top_symbols) < limit / 2: raise Exception(f"CoinGeckoから取得できた銘柄数が少なすぎます ({len(top_symbols)}個)。")

        return top_symbols, "CoinGecko (Market Cap Top)"
        
    except Exception:
        return DEFAULT_SYMBOLS[:limit], "Static List (Fallback)"


async def fetch_ohlcv_async(symbol: str, timeframe: str, limit: int) -> List[list]:
    """OHLCVは固定のCCXTクライアント (Binance) から取得する"""
    if CCXT_CLIENT is None: return []
    
    market_symbol = f"{symbol}/USDT" 

    try:
        return await CCXT_CLIENT.fetch_ohlcv(market_symbol, timeframe, limit=limit)
    except Exception:
        return []

async def fetch_market_sentiment_data_async(symbol: str) -> Dict:
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
    
async def generate_signal_candidate(symbol: str, macro_context: str) -> Optional[Dict]:
    """
    シグナル生成の厳格なチェックを外し、スコアと条件リストを生成する。
    """
    # 1. データ取得と事前チェック
    regime = await determine_market_regime(symbol)
    if regime == "不明": return None
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    if len(ohlcv_15m) < 100: return None
    sentiment = await fetch_market_sentiment_data_async(symbol)
    win_prob = get_ml_prediction(ohlcv_15m, sentiment) # ロングの確率

    # 2. 条件チェックとリスト生成
    criteria_list = {"MATCHED": [], "MISSED": []}
    side = None
    
    # a. 多時間軸の方向性
    is_aligned, trend_direction = await multi_timeframe_confirmation(symbol)
    if is_aligned: 
        criteria_list["MATCHED"].append(f"多時間軸の方向性が一致 ({trend_direction})")
    else:
        criteria_list["MISSED"].append(f"多時間軸の方向性が不一致 ({trend_direction})")

    # b. サイドの決定（トレンドフォロー）
    is_strong_trend = False
    if trend_direction == "上昇" and regime == "強気トレンド":
        side = "ロング"
        is_strong_trend = True
    elif trend_direction == "下降" and regime == "弱気トレンド":
        side = "ショート"
        is_strong_trend = True
        
    if is_strong_trend:
        criteria_list["MATCHED"].append(f"長期レジーム({regime})と短期方向性が一致")
    else:
        criteria_list["MISSED"].append(f"長期レジーム({regime})と短期方向性が不一致")
        
    # c. マクロ経済との整合性 (サイドが決定していない場合はチェック対象外)
    if side is not None:
        if (side == "ロング" and macro_context == "リスクオフ (株安)") or \
           (side == "ショート" and macro_context == "リスクオン (株高)"):
            criteria_list["MISSED"].append(f"マクロ経済が逆行 ({macro_context})")
        else:
            criteria_list["MATCHED"].append(f"マクロ経済と整合 ({macro_context})")
            
    criteria_list["MATCHED"].append("OIデータはニュートラルとして処理されました")
    
    # 3. 実行方向（side）が定まらない場合はシグナル候補から除外
    if side is None:
        return None

    # 4. スコア計算
    # final_confidence: 最終的な信頼度 (ロングなら win_prob, ショートなら 1-win_prob)
    final_confidence = win_prob if side == "ロング" else (1 - win_prob)
    # score: ピックアップのためのスコア (0.5からの絶対乖離: 0.0 ～ 0.5)
    score = abs(win_prob - 0.5) 
    
    price = ohlcv_15m[-1][4]
    
    # SL/TPの計算 (エントリープラン作成のため)
    closes_15m = pd.Series([c[4] for c in ohlcv_15m])
    optimal_entry = closes_15m.ewm(span=9, adjust=False).mean().iloc[-1]
    
    df_15m = pd.DataFrame(ohlcv_15m, columns=['t','o','h','l','c','v'])
    df_15m['tr'] = np.maximum(df_15m['h'] - df_15m['l'], np.maximum(abs(df_15m['h'] - df_15m['c'].shift()), abs(df_15m['l'] - df_15m['c'].shift())))
    atr_15m = df_15m['tr'].rolling(14).mean().iloc[-1]
    
    risk_per_unit = atr_15m * 2.5 # SL幅をATRの2.5倍に設定
    sl = optimal_entry - risk_per_unit if side == "ロング" else optimal_entry + risk_per_unit
    
    return {"symbol": symbol, "side": side, "price": price, "sl": sl,
            "criteria_list": criteria_list, "confidence": final_confidence, "score": score,
            "regime": regime, "ohlcv_15m": ohlcv_15m, "optimal_entry": optimal_entry, "atr_15m": atr_15m}

def format_telegram_message(signal: Dict) -> str:
    """強制通知用に一致/不一致条件を明確に表示するメッセージを作成する"""
    side_icon = "📈" if signal['side'] == "ロング" else "📉"
    
    # スコアが0.15 (信頼度65%)以上なら高信頼度シグナルとして、そうでない場合は「注目」として表示
    if signal['score'] >= 0.15:
        msg = f"💎 <b>Apex BOT シグナル速報: {signal['symbol']}</b> {side_icon}\n"
        msg += f"<i>市場レジーム: {signal['regime']} ({CCXT_CLIENT_NAME.split(' ')[0]}データ)</i>\n"
        msg += f"<i>MLモデル予測信頼度: {signal['confidence']:.2%}</i>\n\n"
    else:
        msg = f"🔔 <b>Apex BOT 注目銘柄: {signal['symbol']}</b> {side_icon} (暫定)\n"
        msg += f"<i>現在の最高スコア銘柄を選定しました。</i>\n"
        msg += f"<i>MLモデル予測信頼度: {signal['confidence']:.2%} (スコア: {signal['score']:.4f})</i>\n\n"
    
    # --- 条件の表示 ---
    msg += "<b>✅ 一致した判断根拠</b>\n"
    if signal['criteria_list']['MATCHED']:
        for c in signal['criteria_list']['MATCHED']: msg += f"• {c}\n"
    else:
        msg += "• なし\n"
        
    msg += "\n<b>❌ 不一致または未確認の条件</b>\n"
    if signal['criteria_list']['MISSED']:
        for c in signal['criteria_list']['MISSED']: msg += f"• {c}\n"
    else:
        msg += "• なし\n"
    # --- エントリープラン ---
        
    price = signal['price']
    optimal_entry = signal['optimal_entry']
    atr_15m = signal['atr_15m']
    sl = signal['sl']
    
    entry_zone_upper = optimal_entry + (atr_15m * 0.5)
    entry_zone_lower = optimal_entry - (atr_15m * 0.5)
    
    risk_per_unit = abs(optimal_entry - sl)
    tp1 = optimal_entry + (risk_per_unit * 1.5) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 1.5)
    tp2 = optimal_entry + (risk_per_unit * 3.0) if signal['side'] == "ロング" else optimal_entry - (risk_per_unit * 3.0)
    
    msg += "\n<b>🎯 精密エントリープラン</b>\n"
    msg += f"<pre>現在価格: {price:,.4f}\n\n"
    if signal['side'] == 'ロング':
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"範囲: {entry_zone_lower:,.4f} 〜 {entry_zone_upper:,.4f}\n"
        msg += "👉 この価格帯への押し目を待ってエントリー\n\n"
    else:
        msg += f"--- エントリーゾーン (指値案) ---\n"
        msg += f"最適: {optimal_entry:,.4f} (9EMA)\n"
        msg += f"範囲: {entry_zone_lower:,.4f} 〜 {entry_zone_upper:,.4f}\n"
        msg += "👉 この価格帯への戻りを待ってエントリー\n\n"
        
    msg += f"--- ゾーンエントリー時の目標 ---\n"
    msg += f"損切 (SL): {sl:,.4f}\n"
    msg += f"利確① (TP1): {tp1:,.4f}\n"
    msg += f"利確② (TP2): {tp2:,.4f}</pre>"
    
    return msg


async def main_loop():
    """BOTの常時監視を実行するバックグラウンドタスク"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS
    
    # 起動時の初期リスト設定
    CURRENT_MONITOR_SYMBOLS, source = await fetch_top_symbols_async(30)
    macro_context = get_tradfi_macro_context()
    LAST_UPDATE_TIME = time.time()
    
    # --- 初期起動時のテスト通知 ---
    await send_test_message()
    # -----------------------------
    
    while True:
        try:
            current_time = time.time()
            is_dynamic_update_needed = (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL
            
            # --- 動的更新フェーズ (5分に一度) ---
            if is_dynamic_update_needed:
                logging.info("==================================================")
                logging.info(f"Apex BOT v6.1 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                
                macro_context = get_tradfi_macro_context() 
                logging.info(f"マクロ経済コンテクスト: {macro_context}")
                
                symbols_to_monitor, source_exchange = await fetch_top_symbols_async(30)
                CURRENT_MONITOR_SYMBOLS = symbols_to_monitor
                LAST_UPDATE_TIME = current_time
                
                logging.info(f"銘柄選定元: {source_exchange}")
                logging.info(f"監視対象 (TOP30): {', '.join(CURRENT_MONITOR_SYMBOLS[:5])} ...")
                logging.info("--------------------------------------------------")
            
            # --- メイン分析実行 (30秒ごと) ---
            
            # 1. 全銘柄のシグナル候補を生成
            candidate_tasks = [generate_signal_candidate(sym, macro_context) for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)
            
            # 2. 候補のフィルタリング (方向性が決定したもののみ)
            valid_candidates = [c for c in candidates if c is not None and c['side'] is not None]

            # 3. 通知が必要な銘柄を特定
            best_signal = None
            if valid_candidates:
                # score (0.5からの乖離度) が最も高いものを選択
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                
                # 通知フィルタリング:
                # a) スコアが最低限 0.10 (信頼度 60%) 以上であること
                # b) 一度通知したら1時間(3600秒)は再通知しないこと
                is_ready_to_notify = best_signal['score'] >= 0.10
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600

                # 4. 通知の実行
                if is_ready_to_notify and is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    send_telegram_html(message, is_emergency=True)
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                    
                    log_msg = f"🚨 強制通知成功: {best_signal['symbol']} - {best_signal['side']} @ {best_signal['price']:.4f} (スコア: {best_signal['score']:.4f})"
                    logging.info(log_msg)
            
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
    
    # 1. CCXTクライアントの初期化 (Binanceに固定)
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
    monitor_info = CURRENT_MONITOR_SYMBOLS[0] if CURRENT_MONITOR_SYMBOLS else "No Symbols"
    return {
        "status": "Running",
        "service": "Apex BOT v6.1 (Forced Signal)",
        "monitoring_base": CCXT_CLIENT_NAME.split(' ')[0],
        "next_dynamic_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }
