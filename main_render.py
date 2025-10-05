# ====================================================================================
# Apex BOT v11.7.1 - 三層時間軸分析統合版 (MACDS_12_26_9 KeyError修正済)
# 最終更新: 2025年10月
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
import random
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 360        # 6分
SYMBOL_WAIT = 0.0          

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 2時間クールダウン
SIGNAL_THRESHOLD = 0.55             # 通常シグナル閾値
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

STRONG_NEUTRAL_MIN_DIFF = 0.02      
LONG_TERM_SMA_LENGTH = 50           # 4時間足SMAの期間
LONG_TERM_REVERSAL_PENALTY = 0.15   # 4hトレンド逆行時のスコア減点

# 短期高勝率特化のための定数調整
MACD_CROSS_PENALTY = 0.08           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.0            
SHORT_TERM_SL_MULTIPLIER = 1.0      


# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_price_utility(price: float, symbol: str) -> str:
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:.2f}"
    if price >= 10: return f"{price:.4f}"
    if price >= 0.1: return f"{price:.6f}"
    return f"{price:.8f}"

def send_telegram_html(message: str) -> bool:
    """TelegramにHTML形式でメッセージを送信する (ダミー実装)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
        logging.info("Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError:
        return False
    except requests.exceptions.RequestException:
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (ダミーロジック)"""
    base_rate = 0.50
    if timeframe == '15m':
        base_rate = 0.58 
        score_multiplier = 0.35
    elif timeframe == '1h':
        base_rate = 0.55 
        score_multiplier = 0.40
    else: # 4h
        base_rate = 0.52 
        score_multiplier = 0.45

    adjusted_rate = base_rate + (score - 0.50) * score_multiplier
    return max(0.40, min(0.80, adjusted_rate))

def generate_individual_analysis_text(signal: Dict) -> str:
    """各時間軸の分析結果を分かりやすいテキスト形式で生成"""
    
    timeframe = signal.get('timeframe', 'N/A')
    side = signal.get('side', 'Neutral')
    score = signal.get('score', 0.5)
    tech_data = signal.get('tech_data', {})
    
    estimated_win_rate = get_estimated_win_rate(score, timeframe)
    
    # 総合判断と推奨アクション
    action = "様子見"
    if score >= 0.75:
        action = f"**強い推奨 ({side})**"
    elif score >= 0.65:
        action = f"推奨 ({side})"
    elif score < 0.40:
        action = f"要注意 ({'ロング' if side == 'ショート' else 'ショート'})"
        
    # 主要根拠の抽出
    adx = tech_data.get('adx', 25.0)
    rsi = tech_data.get('rsi', 50.0)
    macd_hist = tech_data.get('macd_hist', 0.0)
    
    # 根拠の文章化
    reasons = []
    
    # トレンド/レンジ
    regime = "トレンド" if adx >= 25 else "レンジ"
    reasons.append(f"市場レジーム: {regime} (ADX: {adx:.1f})")

    # モメンタム (RSI)
    if rsi >= 70 or rsi <= 30:
        reasons.append(f"RSI: {rsi:.1f} → 過熱状態")
    elif (side == 'ロング' and rsi > 55) or (side == 'ショート' and rsi < 45):
        reasons.append(f"RSI: {rsi:.1f} → モメンタム追従")
    else:
        reasons.append(f"RSI: {rsi:.1f} → 中立的")

    # 勢い (MACD Hist)
    if abs(macd_hist) > 0.005:
        reasons.append(f"MACD Hist: {macd_hist:+.4f} → 強い勢い")
    elif abs(macd_hist) > 0.0005:
        reasons.append(f"MACD Hist: {macd_hist:+.4f} → 緩やかな勢い")
    else:
        reasons.append(f"MACD Hist: {macd_hist:+.4f} → 勢いなし")

    # 長期トレンドフィルター (4h SMA)
    if timeframe != '4h':
        long_term_trend = tech_data.get('long_term_trend', 'Neutral')
        if tech_data.get('long_term_reversal_penalty', False):
            reasons.append(f"長期トレンド: {long_term_trend} (逆行減点済)")
        else:
            reasons.append(f"長期トレンド: {long_term_trend} (追従)")

    # MACDクロス (15mのみ)
    if timeframe == '15m':
        macd_valid = tech_data.get('macd_cross_valid', False)
        reasons.append(f"MACDクロス: {'確認済' if macd_valid else '未確認'} (エントリー確実性)")
        
    
    return (
        f"**アクション**: {action}\n"
        f"**推定勝率**: {estimated_win_rate * 100:.1f}%\n"
        f"**スコア**: {score:.4f}\n"
        f"**根拠**: {' / '.join(reasons)}"
    )

def format_integrated_analysis_message(symbol: str, signals: List[Dict]) -> str:
    """3つの時間軸の分析結果を統合し、Telegram通知メッセージを整形"""
    
    short_signal = next((s for s in signals if s.get('timeframe') == '15m'), None)
    mid_signal = next((s for s in signals if s.get('timeframe') == '1h'), None)
    long_signal = next((s for s in signals if s.get('timeframe') == '4h'), None)

    price = short_signal['price'] if short_signal else (mid_signal['price'] if mid_signal else 0.0)
    format_price = lambda p: format_price_utility(p, symbol)

    # 最もスコアの高いシグナルを特定し、取引計画を提示
    best_signal = max(signals, key=lambda s: s.get('score', 0.5)) if signals else None
    
    trade_plan_section = ""
    if best_signal and best_signal.get('score', 0.5) >= SIGNAL_THRESHOLD:
        tp_price = best_signal.get('tp1', 0.0)
        sl_price = best_signal.get('sl', 0.0)
        entry_price = best_signal.get('entry', 0.0)
        rr_ratio = best_signal.get('rr_ratio', 0.0)
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'N/A')

        trade_plan_section = (
            f"**🔥 {timeframe}足 ({side}) に基づく推奨取引計画**\n"
            f"| 指標 | 価格 | 設定・目標 |\n"
            f"| :--- | :--- | :--- |\n"
            f"| **推奨エントリー (Entry)** | <code>${format_price(entry_price)}</code> | 価格:\n"
            f"| 🟢 **利確目標 (TP)** | <code>${format_price(tp_price)}</code> | SLの {rr_ratio:.2f} 倍\n"
            f"| 🔴 **損切位置 (SL)** | <code>${format_price(sl_price)}</code> | ATRの {SHORT_TERM_SL_MULTIPLIER:.1f} 倍\n"
            f"| **リスクリワード比 (RRR)** | **1:{rr_ratio:.2f}** | {'長期追従ボーナス適用' if rr_ratio > SHORT_TERM_BASE_RRR else '短期基本設定'} |\n"
            f"---------------------------------------\n"
        )
    
    header = (
        f"🎯 <b>{symbol} - 三層時間軸 統合分析レポート</b> 📊\n"
        f"---------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(price)}</code>\n"
        f"• <b>データ元</b>: {CCXT_CLIENT_NAME}\n"
        f"---------------------------------------\n"
    ) + trade_plan_section

    # 短期分析
    short_analysis = generate_individual_analysis_text(short_signal) if short_signal else "データ不足のため分析不能。"
    
    # 中期分析
    mid_analysis = generate_individual_analysis_text(mid_signal) if mid_signal else "データ不足のため分析不能。"
    
    # 長期分析
    long_analysis = generate_individual_analysis_text(long_signal) if long_signal else "データ不足のため分析不能。"

    body = (
        f"**📈 1. 短期分析 (15m) - エントリータイミング重視**\n"
        f"{short_analysis}\n\n"
        f"**🗓️ 2. 中期分析 (1h) - トレンド継続性重視**\n"
        f"{mid_analysis}\n\n"
        f"**🌍 3. 長期分析 (4h) - マクロトレンド重視**\n"
        f"{long_analysis}\n\n"
    )
    
    # 総合的な取引推奨
    short_side = short_signal.get('side', 'Neutral') if short_signal else 'Neutral'
    long_side = long_signal.get('side', 'Neutral') if long_signal else 'Neutral'
    
    overall_recommendation = "様子見"
    if short_side == long_side and short_side != 'Neutral' and short_signal and long_signal and short_signal['score'] >= 0.65:
        overall_recommendation = f"**強い統一推奨 ({short_side})**: 短期と長期の方向性が一致"
    elif short_side != 'Neutral' and short_signal and short_signal['score'] >= 0.75:
        overall_recommendation = f"短期高確度推奨 ({short_side}): 長期トレンドと乖離がある場合は注意"
    else:
        overall_recommendation = "方向性不一致または確度低。安全なトレードは推奨しません。"
        
    footer = (
        f"---"
        f"🔥 <b>BOTの総合取引推奨</b>:\n"
        f"{overall_recommendation}"
    )

    return header + body + footer


# ====================================================================================
# CCXT & DATA ACQUISITION (ダミー/基本ロジック)
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (ダミー)"""
    logging.info("CCXTクライアントを初期化しました (ダミー)")

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """
    OHLCVデータ取得 (ダミー実装)
    """
    try:
        if timeframe == '4h':
            data_length = 60 # 60本で約10日分
        elif timeframe == '1h':
            data_length = 100
        else: # 15m
            data_length = 100

        # ダミーデータ生成
        data = []
        base_price = 3000.0 if symbol == "ETH/USDT" else 60000.0
        for i in range(data_length):
            trend = np.sin(i / 10) * 5 + np.cos(i / 5) * 2
            close = base_price + trend + random.uniform(-0.5, 0.5)
            ts_interval = {'15m': 900000, '1h': 3600000, '4h': 14400000}.get(timeframe, 900000)
            data.append([time.time() * 1000 - (data_length - i) * ts_interval, close, close + 1, close - 1, close, 1000.0])
            
        return data, "Success", client_name
    except Exception:
        return [], "DataShortage", client_name

async def get_crypto_macro_context() -> Dict:
    """マクロ市場コンテキストを取得 (ダミー)"""
    return {
        "vix_value": 2.5,
        "trend": "Risk-On (BTC Dominance stable)"
    }


# ====================================================================================
# CORE ANALYSIS LOGIC (時間軸ごとの分離)
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['rsi'] = ta.rsi(df['close'], length=14)
    df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
    df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
    df.ta.bbands(close='close', length=20, append=True)
    
    price = df['close'].iloc[-1]
    
    # 2. 基本シグナル判断ロジック（ダミー）
    base_score = 0.5 
    if df['rsi'].iloc[-1] > 70:
        side = "ショート"
        base_score = 0.70 + random.uniform(0.01, 0.15) 
    elif df['rsi'].iloc[-1] < 30:
        side = "ロング"
        base_score = 0.70 + random.uniform(0.01, 0.15)
    else:
        side = "Neutral"
        base_score = 0.5 + random.uniform(-0.045, 0.045) 
        if base_score > 0.5: side = "ロング"
        elif base_score < 0.5: side = "ショート"
        else: side = "Neutral"
        
    score = base_score
    
    # 3. 4hトレンドフィルターの適用 (15m, 1hのみ)
    current_long_term_penalty_applied = False
    if timeframe in ['15m', '1h']:
        if (side == "ロング" and long_term_trend == "Short") or \
           (side == "ショート" and long_term_trend == "Long"):
            score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
            current_long_term_penalty_applied = True
    
    # 4. MACDクロス確認と減点 (15mのみ) <- ★ 修正箇所
    macd_valid = False
    
    # MACDラインとシグナルラインの存在チェックを最初に行う
    if 'MACD_12_26_9' in df.columns and 'MACDS_12_26_9' in df.columns and len(df) >= 2:
        
        macd_line = df['MACD_12_26_9']
        signal_line = df['MACDS_12_26_9']
        
        if timeframe == '15m':
            # MACDがシグナルラインをクロスした瞬間をチェック
            is_long_cross = (macd_line.iloc[-2] < signal_line.iloc[-2]) and (macd_line.iloc[-1] >= signal_line.iloc[-1])
            is_short_cross = (macd_line.iloc[-2] > signal_line.iloc[-2]) and (macd_line.iloc[-1] <= signal_line.iloc[-1])
            
            if (side == "ロング" and is_long_cross) or (side == "ショート" and is_short_cross):
                macd_valid = True
            
    # MACDクロスが確認できない場合、スコアを減点 (高勝率化)
    # MACDのチェックは15mのみに適用される
    if not macd_valid and score >= SIGNAL_THRESHOLD and timeframe == '15m':
        score = max(0.5, score - MACD_CROSS_PENALTY)
            
    # 5. TP/SLとRRRの決定 (短期高勝率ルールを適用)
    atr_val = price * 0.005 # ダミーATR値
    
    rr_base = SHORT_TERM_BASE_RRR 
    
    # 長期トレンド追従の場合、RRRをボーナス加算
    if (timeframe != '4h') and (side == long_term_trend):
        rr_base = SHORT_TERM_MAX_RRR
    
    # SL幅をATRの1.0倍にタイト化
    sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
    tp_dist = sl_dist * rr_base 

    if side == "ロング":
        entry = price * 0.9995 
        sl = entry - sl_dist
        tp1 = entry + tp_dist
    elif side == "ショート":
        entry = price * 1.0005 
        sl = entry + sl_dist
        tp1 = entry - tp_dist
    else:
        entry, sl, tp1, rr_base = 0, 0, 0, 0
    
    # 6. 最終的なサイドの決定
    final_side = side
    if score < SIGNAL_THRESHOLD and score > (1.0 - SIGNAL_THRESHOLD):
         if abs(score - 0.5) < STRONG_NEUTRAL_MIN_DIFF: 
             final_side = "Neutral"
    elif score < (1.0 - SIGNAL_THRESHOLD): 
         final_side = "Neutral"

    # 7. シグナル辞書を構築
    tech_data = {
        "rsi": df['rsi'].iloc[-1] if not df.empty and df['rsi'].iloc[-1] is not None else 50.0,
        "macd_hist": df['MACDH_12_26_9'].iloc[-1] if not df.empty and df['MACDH_12_26_9'].iloc[-1] is not None else 0.0,
        "adx": df['adx'].iloc[-1] if not df.empty and df['adx'].iloc[-1] is not None else 25.0,
        "bb_width_pct": (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / df['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df.columns else 0.0,
        "atr_value": atr_val,
        "long_term_trend": long_term_trend,
        "long_term_reversal_penalty": current_long_term_penalty_applied,
        "macd_cross_valid": macd_valid,
    }
    
    signal_candidate = {
        "symbol": symbol,
        "side": final_side,
        "score": score,
        "confidence": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0,
        "regime": "トレンド" if tech_data['adx'] >= 25 else "レンジ",
        "macro_context": macro_context,
        "client": client_used,
        "timeframe": timeframe,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    """
    3つの時間軸のシグナルを統合して生成する
    """
    
    # 0. 4hトレンドの事前計算 (他の短期・中期分析のフィルターとして利用)
    long_term_trend = 'Neutral'
    
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    if status_4h == "Success":
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_4h['close'] = pd.to_numeric(df_4h['close'])
        df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
        
        if not df_4h.empty and df_4h['sma'].iloc[-1] is not None:
            last_price = df_4h['close'].iloc[-1]
            last_sma = df_4h['sma'].iloc[-1]
            
            if last_price > last_sma:
                long_term_trend = 'Long'
            elif last_price < last_sma:
                long_term_trend = 'Short'
            
    # 1. 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend, False)
    ]
    
    results = await asyncio.gather(*tasks)
    
    # 4h分析結果の統合: 4hシグナルは他の短期・中期の分析結果を上書きしない
    for result in results:
        if result and result.get('timeframe') == '4h':
            result['tech_data']['long_term_trend'] = long_term_trend
    
    return [r for r in results if r is not None]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP
# ====================================================================================

async def notify_integrated_analysis(symbol: str, signals: List[Dict]):
    """統合分析レポートをTelegramに送信"""
    global TRADE_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    # いずれかの時間軸でスコアが0.65以上のシグナルがあれば通知
    if any(s.get('score', 0.5) >= 0.65 for s in signals):
        # 統合分析レポートのクールダウンは、銘柄ごとに1時間に設定
        if current_time - TRADE_NOTIFIED_SYMBOLS.get(f"{symbol}_INTEGRATED", 0) > 60 * 60:
            logging.info(f"📰 統合分析レポートを通知: {symbol}")
            TRADE_NOTIFIED_SYMBOLS[f"{symbol}_INTEGRATED"] = current_time
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_integrated_analysis_message(symbol, signals))))


async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CCXT_CLIENT_NAME

    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            macro_context = await get_crypto_macro_context()
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)}, クライアント: {CCXT_CLIENT_NAME})")
            
            # 各銘柄に対して統合シグナル生成タスクを実行
            tasks = [generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME) for symbol in monitor_symbols]
            
            # 全銘柄の分析を並行して実行
            results_list_of_lists = await asyncio.gather(*tasks)
            
            # 結果を平坦化
            LAST_ANALYSIS_SIGNALS = [s for sublist in results_list_of_lists for s in sublist if s is not None and s.get('side') not in ["DataShortage", "ExchangeError"]]

            # 統合レポートの通知
            for symbol_results in results_list_of_lists:
                if symbol_results and any(s.get('side') != "DataShortage" for s in symbol_results):
                    asyncio.create_task(notify_integrated_analysis(symbol_results[0]['symbol'], symbol_results))
            
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            # エラー発生時もクラッシュせず、ログを残して次のループへ
            logging.error(f"メインループで致命的なエラー: {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v11.7.1-TRIPLE_ANALYSIS_FIX (Full Integrated)")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v11.7.1 Startup initializing...") 
    # バックグラウンドでメインループを実行
    asyncio.create_task(main_loop())

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v11.7.1-TRIPLE_ANALYSIS_FIX (Full Integrated)",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v11.7.1, Full Integrated)."}, status_code=200)

if __name__ == '__main__':
    # 実行環境に応じてポートとホストを調整してください
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
