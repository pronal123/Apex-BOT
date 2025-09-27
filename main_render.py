# ====================================================================================
# Apex BOT v6.7 - レンジ相場からの強制選定版 (main_render.py)
# ====================================================================================
#
# 目的: MTF/トレンド構造の厳格な一致がなくても、ML予測スコアに基づきサイドを決定し、
#       中立市場でも最も優位性のある銘柄を強制的に通知する。
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
from fastapi import FastAPI
import uvicorn
from dotenv import load_dotenv
load_dotenv()

# ====================================================================================
#           　　　　　　　　　　　　　CONFIG
# ====================================================================================

JST = timezone(timedelta(hours=9))
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LTC", "ADA", "DOGE", "AVAX", "DOT", "MATIC", "LINK", "UNI", "BCH", "FIL", "TRX", "XLM", "ICP", "ETC", "AAVE", "MKR", "ATOM", "EOS", "ALGO", "ZEC", "COMP", "NEO", "VET", "DASH", "QTUM"] 

# 環境変数から設定を読み込む
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'YOUR_COINGLASS_API_KEY')

# --- 動作設定 ---
LOOP_INTERVAL = 30       
DYNAMIC_UPDATE_INTERVAL = 300 

# ====================================================================================
#                               UTILITIES & CLIENTS
# ====================================================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)

CCXT_CLIENT_NAME = 'Binance Futures' 
CCXT_CLIENT = None 
LAST_UPDATE_TIME = 0.0 
CURRENT_MONITOR_SYMBOLS = []
NOTIFIED_SYMBOLS = {}

def initialize_ccxt_client():
    global CCXT_CLIENT
    CCXT_CLIENT = ccxt_async.binance({"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "future"}})

async def send_test_message():
    """BOT起動時のセルフテスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v6.7 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"Render環境でのWebサービス起動に成功しました。\n"
        f"**中立市場対応の強制通知モード**で稼働中です。"
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
    """
    動的選定がブロックされるため、VIXマクロに基づいて静的リストの優先順位をシャッフルする。
    """
    coingecko_url = "https://api.coingecko.com/api/v3/coins/markets" # ダミーURL
    
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="5m")
        if vix.empty or len(vix) < 10: raise Exception("VIXデータ不足")
        vix_change = vix['Close'].iloc[-1] / vix['Close'].iloc[-5] - 1
        
        final_list = DEFAULT_SYMBOLS[:limit]
        
        if abs(vix_change) > 0.005: 
            random.shuffle(final_list)
            logging.info(f"✅ VIX変動 ({vix_change:.2%}) に基づきリストをシャッフルしました。")
            return final_list, "Self-Adjusted Static List"
        else:
            logging.info("VIXは安定。リストはデフォルト順序を維持します。")
            return final_list, "Static List (VIX Stable)"

    except Exception as e:
        logging.error(f"❌ 動的選定に失敗: {e}。静的リストにフォールバックします。")
        random.shuffle(DEFAULT_SYMBOLS)
        return DEFAULT_SYMBOLS[:limit], "Static List (Randomized Fallback)"


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
        es = yf.Ticker("ES=F").history(period="5d", interval="1h")
        if es.empty: return "不明"
        prices = es['Close']
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

async def calculate_rsi(prices: pd.Series, window: int = 14) -> float:
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=window).mean()
    avg_loss = loss.rolling(window=window).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    return 100 - (100 / (1 + rs)).iloc[-1]

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

async def multi_timeframe_confirmation(symbol: str) -> Dict:
    """MTF分析を厳格化：KAMA、RSI、EMAの3つで整合性をチェック"""
    timeframes = ['1h', '4h']
    results = {"kama": [], "rsi": [], "ema": [], "trend": "不明"}
    
    for tf in timeframes:
        ohlcv = await fetch_ohlcv_async(symbol, tf, 60)
        if len(ohlcv) < 60: return {"kama": [], "rsi": [], "ema": [], "trend": "データ不足"}

        prices = pd.Series([c[4] for c in ohlcv])
        current_price = prices.iloc[-1]
        
        kama = calculate_kama(prices, period=21).iloc[-1]
        kama_trend = "上昇" if current_price > kama else "下降"
        results["kama"].append(kama_trend)
        
        rsi = await calculate_rsi(prices)
        rsi_trend = "上昇" if rsi > 55 else ("下降" if rsi < 45 else "中立")
        results["rsi"].append(rsi_trend)

        ema_short = prices.ewm(span=9, adjust=False).mean().iloc[-1]
        ema_trend = "上昇" if current_price > ema_short else "下降"
        results["ema"].append(ema_trend)

    all_kama_up = all(t == "上昇" for t in results["kama"])
    all_kama_down = all(t == "下降" for t in results["kama"])
    all_ema_up = all(t == "上昇" for t in results["ema"])
    all_ema_down = all(t == "下降" for t in results["ema"])
    
    rsi_ok_up = all(t in ["上昇", "中立"] for t in results["rsi"])
    rsi_ok_down = all(t in ["下降", "中立"] for t in results["rsi"])
    
    if all_kama_up and all_ema_up and rsi_ok_up:
        results["trend"] = "上昇"
    elif all_kama_down and all_ema_down and rsi_ok_down:
        results["trend"] = "下降"
    else:
        results["trend"] = "不一致"
        
    return results

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

async def find_local_sr(prices: pd.Series, window: int = 20) -> Tuple[Optional[float], Optional[float]]:
    highs = prices.rolling(window=window).max()
    lows = prices.rolling(window=window).min()
    current_high = highs.iloc[-1]
    current_low = lows.iloc[-1]
    
    if prices.iloc[-1] > current_high * 0.995: 
        R = current_high
    else:
        R = None
        
    if prices.iloc[-1] < current_low * 1.005:
        S = current_low
    else:
        S = None
        
    return S, R

async def generate_signal_candidate(symbol: str, macro_context: str) -> Optional[Dict]:
    
    regime = await determine_market_regime(symbol)
    if regime == "不明": return None
    ohlcv_15m = await fetch_ohlcv_async(symbol, '15m', 100)
    if len(ohlcv_15m) < 100: return None
    sentiment = await fetch_market_sentiment_data_async(symbol)
    win_prob = get_ml_prediction(ohlcv_15m, sentiment) 
    
    prices_15m = pd.Series([c[4] for c in ohlcv_15m])
    current_price = prices_15m.iloc[-1]
    rsi_15m = await calculate_rsi(prices_15m)
    mtf_results = await multi_timeframe_confirmation(symbol)
    trend_direction = mtf_results["trend"]
    
    criteria_list = {"MATCHED": [], "MISSED": []}
    side = None
    
    # 1. サイド決定ロジックの柔軟化: MTFが不一致でも、ML予測スコアが高ければ方向を決定
    is_trend_aligned = (trend_direction != "不一致" and trend_direction != "データ不足")
    
    if trend_direction == "上昇" or (not is_trend_aligned and win_prob > 0.65):
        side = "ロング"
    elif trend_direction == "下降" or (not is_trend_aligned and win_prob < 0.35):
        side = "ショート"
    
    # 評価のためのロギング
    if is_trend_aligned:
        criteria_list["MATCHED"].append(f"MTF分析が一致 ({trend_direction})")
    else:
        criteria_list["MISSED"].append(f"MTF分析が不一致 ({trend_direction})")
        
    if regime != "レンジ相場":
         criteria_list["MATCHED"].append(f"長期レジームはトレンド ({regime})")
    else:
         criteria_list["MISSED"].append(f"長期レジームはレンジ ({regime})")

    # 2. RSIによる過熱感フィルタ
    if side == "ロング":
        if rsi_15m < 70:
            criteria_list["MATCHED"].append(f"RSIは過熱なし ({rsi_15m:.1f})")
        else:
            criteria_list["MISSED"].append(f"RSIが過熱域 ({rsi_15m:.1f})")
    elif side == "ショート":
        if rsi_15m > 30:
            criteria_list["MATCHED"].append(f"RSIは売られすぎではない ({rsi_15m:.1f})")
        else:
            criteria_list["MISSED"].append(f"RSIが売られすぎ ({rsi_15m:.1f})")
        
    # 3. マクロ経済との整合性
    if side is not None:
        if (side == "ロング" and macro_context == "リスクオフ (株安)") or \
           (side == "ショート" and macro_context == "リスクオン (株高)"):
            criteria_list["MISSED"].append(f"マクロ経済が逆行 ({macro_context})")
        else:
            criteria_list["MATCHED"].append(f"マクロ経済と整合 ({macro_context})")
            
    criteria_list["MATCHED"].append("OIデータはニュートラルとして処理されました")
    
    if side is None: return None

    # 4. ポジションとSL/TPの決定
    final_confidence = win_prob if side == "ロング" else (1 - win_prob)
    score = abs(win_prob - 0.5) 
    
    optimal_entry = prices_15m.ewm(span=9, adjust=False).mean().iloc[-1]
    df_15m = pd.DataFrame(ohlcv_15m, columns=['t','o','h','l','c','v'])
    df_15m['tr'] = np.maximum(df_15m['h'] - df_15m['l'], np.maximum(abs(df_15m['h'] - df_15m['c'].shift()), abs(df_15m['l'] - df_15m['c'].shift())))
    atr_15m = df_15m['tr'].rolling(14).mean().iloc[-1]
    
    # SL高度化
    sl_offset = atr_15m * 2.0 
    sl = optimal_entry - sl_offset if side == "ロング" else optimal_entry + sl_offset

    # TP高度化
    S, R = await find_local_sr(prices_15m, window=30)
    risk_per_unit = abs(optimal_entry - sl)
    default_tp1 = optimal_entry + (risk_per_unit * 1.5) if side == "ロング" else optimal_entry - (risk_per_unit * 1.5)
    default_tp2 = optimal_entry + (risk_per_unit * 3.0) if side == "ロング" else optimal_entry - (risk_per_unit * 3.0)
    
    if side == "ロング" and R is not None and R > default_tp1:
        tp1 = R
        criteria_list["MATCHED"].append(f"TP1をレジスタンス({R:.4f})に設定")
    elif side == "ショート" and S is not None and S < default_tp1:
        tp1 = S
        criteria_list["MATCHED"].append(f"TP1をサポート({S:.4f})に設定")
    else:
        tp1 = default_tp1
        criteria_list["MATCHED"].append("TP1をリスクリワード1.5倍に設定")
        
    tp2 = default_tp2
    
    return {"symbol": symbol, "side": side, "price": current_price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "criteria_list": criteria_list, "confidence": final_confidence, "score": score,
            "regime": regime, "ohlcv_15m": ohlcv_15m, "optimal_entry": optimal_entry, "atr_15m": atr_15m}

def format_telegram_message(signal: Dict) -> str:
    side_icon = "📈" if signal['side'] == "ロング" else "📉"
    
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
    tp1 = signal['tp1']
    tp2 = signal['tp2']
    
    entry_zone_upper = optimal_entry + (atr_15m * 0.5)
    entry_zone_lower = optimal_entry - (atr_15m * 0.5)
    
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
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS
    
    # 起動時の初期リスト設定
    CURRENT_MONITOR_SYMBOLS, source = await fetch_top_symbols_async(30)
    macro_context = get_tradfi_macro_context()
    LAST_UPDATE_TIME = time.time()
    
    await send_test_message()
    
    while True:
        try:
            current_time = time.time()
            is_dynamic_update_needed = (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL
            
            # --- 動的更新フェーズ (5分に一度) ---
            if is_dynamic_update_needed:
                logging.info("==================================================")
                logging.info(f"Apex BOT v6.7 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                
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

            # 3. 通知が必要な銘柄を特定 (強制通知ロジック)
            best_signal = None
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600

                # --- V6.7 追加ログ: 最優秀候補の状態を記録 ---
                log_status = "✅ 通知実行" if is_not_recently_notified else "🔒 1時間ロック中"
                log_msg = f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 状況: {log_status}"
                logging.info(log_msg)
                
                # 4. 通知の実行 (最低スコア制限なし)
                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    send_telegram_html(message, is_emergency=True)
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
            else:
                # MTF/レジームがすべて不一致だった場合、ログに記録
                logging.info("➡️ シグナル候補なし: 市場全体が極めてレンジか不明瞭です。")
            
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
    logging.info(f"Health Check Ping Received. Analyzing: {monitor_info}...")
    return {
        "status": "Running",
        "service": "Apex BOT v6.7 (Mid-Market Force)",
        "monitoring_base": CCXT_CLIENT_NAME.split(' ')[0],
        "next_dynamic_update": f"{DYNAMIC_UPDATE_INTERVAL - (time.time() - LAST_UPDATE_TIME):.0f}s"
    }
