# ====================================================================================
# Apex BOT v12.0.0 - 動的ロジック統合版
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
import random # 残っている乱数使用箇所は削除または置き換え
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

# 初期リストはOKXの現物形式からスワップ形式 (BTC-USDT) に変換して使用
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      # 出来高で選出する銘柄数
LOOP_INTERVAL = 360        # 6分間隔で分析を実行

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 2時間クールダウン
SIGNAL_THRESHOLD = 0.65             # 通知対象となる最低シグナル閾値 
TOP_SIGNAL_COUNT = 3                # 通知する上位銘柄数
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

STRONG_NEUTRAL_MIN_DIFF = 0.02      
LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.15   

MACD_CROSS_PENALTY = 0.08           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.5            # RRRを調整
SHORT_TERM_SL_MULTIPLIER = 1.5      # SLをATRの1.5倍に調整

# スコアリングロジック用の定数 (v12.0.0で追加/変更)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 45 # RSIが45-55を抜けたかで勢い判定
RSI_MOMENTUM_HIGH = 55
ADX_TREND_THRESHOLD = 25
BASE_SCORE = 0.55  # 基本シグナルが成立した場合のベーススコア

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
# 初期リストもOKXスワップ形式に変換 (BTC/USDT -> BTC-USDT)
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
# 前回成功した動的リストを保存する変数 (フォールバック用)
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()

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
    if price >= 1000: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.4f}"
    if price >= 0.1: return f"{price:,.6f}"
    return f"{price:,.8f}"

def send_telegram_html(message: str) -> bool:
    """TelegramにHTML形式でメッセージを送信する"""
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
    except requests.exceptions.HTTPError as e:
        logging.error(f"Telegram HTTP Error: {e.response.text if e.response else 'N/A'}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Request Error: {e}")
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (ロジック強化)"""
    # スコアに基づいた推定勝率。0.65を基準に線形調整。
    # 0.5 -> 50% / 0.7 -> 65% / 0.8 -> 70% 
    adjusted_rate = 0.50 + (score - 0.50) * 1.5 
    return max(0.40, min(0.80, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict]) -> str:
    """
    3つの時間軸の分析結果を統合し、簡潔で見やすいTelegram通知メッセージを整形 
    (v12.0.0: RRRの表示強化)
    """
    
    # 有効なシグナル（エラーやNeutralではない）のみを抽出
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    # 最高の取引シグナル（最もスコアが高いもの）を取得
    best_signal = max(valid_signals, key=lambda s: s.get('score', 0.5))
    
    if best_signal.get('score', 0.5) < SIGNAL_THRESHOLD:
        return "" 

    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)

    # 根拠セクションの構築 (15m, 1h, 4hの判断を簡潔に並べる)
    analysis_parts = []
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        
        tech_data = s.get('tech_data', {}) # 安全なアクセス
        
        # 4hトレンドの簡易表示
        if tf == '4h':
            long_trend = tech_data.get('long_term_trend', 'Neutral')
            analysis_parts.append(f"🌍 4h (長期): {long_trend}")
        # 短期/中期
        elif s_score >= 0.70:
            analysis_parts.append(f"🔥 {tf} ({s_score:.2f}): **{s_side}**")
        elif s_score >= 0.60:
            analysis_parts.append(f"📈 {tf} ({s_score:.2f}): {s_side}")
        elif s_score <= 0.40:
            analysis_parts.append(f"📉 {tf} ({s_score:.2f}): {s_side}")
        else:
            analysis_parts.append(f"⚖️ {tf} ({s_score:.2f}): {s_side}")
            
    analysis_summary = " / ".join(analysis_parts)
    
    # OKX形式のシンボル (BTC-USDT) を標準形式 (BTC/USDT) に戻して表示
    display_symbol = symbol.replace('-', '/')
    
    # メッセージ本体の構築
    header = (
        f"🎯 <b>高確度取引シグナル ({side})</b> 📊\n"
        f"---------------------------------------\n"
        f"| 銘柄: <b>{display_symbol}</b> | 時間軸: {timeframe} | スコア: <b>{score:.4f}</b> |\n"
        f"| RRR: 1:{rr_ratio:.2f} | 勝率予測: {get_estimated_win_rate(score, timeframe) * 100:.1f}% |\n"
        f"---------------------------------------\n"
    )

    trade_plan = (
        f"**🔥 推奨取引計画 (ベース: {timeframe}足)**\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 **現在価格** | <code>${format_price_utility(price, symbol)}</code> | ({CCXT_CLIENT_NAME}) |\n"
        f"| 🚀 **推奨Entry** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}エントリー |\n"
        f"| 🟢 **利確目標 (TP)** | <code>${format_price_utility(tp_price, symbol)}</code> | RRR 1:{rr_ratio:.2f} |\n"
        f"| 🔴 **損切位置 (SL)** | <code>${format_price_utility(sl_price, symbol)}</code> | SL={SHORT_TERM_SL_MULTIPLIER:.1f} x ATR |\n"
        f"---------------------------------------\n"
    )
    
    analysis_detail = f"**📊 総合分析サマリー**\n{analysis_summary}\n"

    footer = f"\n<pre>現在の市場に最適な高勝率シグナルです。リスク管理を徹底してください。</pre>"

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    # CCXTの非同期クライアントを初期化
    EXCHANGE_CLIENT = ccxt_async.okx({
        'timeout': 20000, 
        'enableRateLimit': True,
        # OKXの無期限スワップ/先物をデフォルトとする
        'options': {'defaultType': 'swap'} 
    })
    
    if EXCHANGE_CLIENT:
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - リアル接続, Default: Swap)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


def convert_symbol_to_okx_swap(symbol: str) -> str:
    """
    USDT現物シンボル (BTC/USDT) をOKXの無期限スワップシンボル (BTC-USDT) に変換する
    """
    # BTC/USDT -> BTC-USDT
    return symbol.replace('/', '-')


async def update_symbols_by_volume():
    """
    CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する
    """
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが未初期化のため、出来高による銘柄更新をスキップします。")
        return

    try:
        # Spot市場のティッカーを取得
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        # USDTペアをフィルタリングし、出来高順にソート
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }

        # quoteVolume (USDTベースの出来高) で降順ソート
        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        # 上位TOP_SYMBOL_LIMIT個を選出し、スワップシンボル形式に変換
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            # 成功時: 現在の監視リストと成功リストを更新
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
            logging.info(f"✅ 出来高TOP30銘柄をOKXスワップ形式に更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
        else:
            # 失敗時: 前回成功したリストを使用する
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning("⚠️ 出来高データを取得できませんでした。前回成功したリストを使用します。")

    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        # 失敗時: 前回成功したリストを使用する
        CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
        logging.warning("⚠️ 出来高データ取得エラー。前回成功したリストにフォールバックします。")

        
async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """
    CCXTを使用してOHLCVデータを取得し、エラー発生時にフォールバックする
    """
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが初期化されていません。")
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        
        # CCXTクライアントのdefaultType='swap'に従うため、symbolはBTC-USDTのような形式
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        logging.warning(f"CCXT Network Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        logging.warning(f"CCXT Exchange Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
    except Exception as e:
        logging.error(f"予期せぬデータ取得エラー ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """マクロ市場コンテキストを取得 (ダミー)"""
    return {
        "vix_value": 2.5,
        "trend": "Risk-On (BTC Dominance stable)"
    }


# ====================================================================================
# CORE ANALYSIS LOGIC (動的ロジック v12.0.0)
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (動的ロジック v12.0.0)
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    # エラー時のための安全なデフォルトtech_data
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    
    price = df['close'].iloc[-1] if not df.empty else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 

    # 初期設定
    final_side = "Neutral"
    base_score = 0.5
    macd_valid = False
    current_long_term_penalty_applied = False
    
    # ----------------------------------------------------
    # 🚨 テクニカル指標の計算
    # ----------------------------------------------------
    try:
        df['rsi'] = ta.rsi(df['close'], length=14)
        df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True)
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        
        rsi_val = df['rsi'].iloc[-1]
        macd_hist_val = df['MACDH_12_26_9'].iloc[-1]
        adx_val = df['adx'].iloc[-1]
        atr_val = df['atr'].iloc[-1] if not pd.isna(df['atr'].iloc[-1]) else atr_val
        
        # 2. **動的シグナル判断ロジック (スコアリング)**
        long_score = 0.5
        short_score = 0.5
        
        # A. MACDに基づく方向性
        if macd_hist_val > 0 and macd_hist_val > df['MACDH_12_26_9'].iloc[-2]:
            long_score += 0.20 # MACDヒストグラム増加 (勢い増)
        elif macd_hist_val < 0 and macd_hist_val < df['MACDH_12_26_9'].iloc[-2]:
            short_score += 0.20 # MACDヒストグラム減少 (勢い増)

        # B. RSIに基づく買われすぎ/売られすぎ
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.10
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.10
            
        # C. RSIに基づくモメンタムブレイクアウト (中立域抜け)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.10
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.10

        # D. ADXに基づくトレンドフォロー強化
        if adx_val > ADX_TREND_THRESHOLD:
            # トレンド相場では、MACD方向へのスコアをさらに強化
            if long_score > short_score:
                long_score += 0.05
            elif short_score > long_score:
                short_score += 0.05
        
        # 最終スコア決定
        if long_score > short_score:
            side = "ロング"
            base_score = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score = short_score
        else:
            side = "Neutral"
            base_score = 0.5

        score = base_score
        
        # 3. 4hトレンドフィルターの適用 (15m, 1hのみ)
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                # 逆張りペナルティ: 厳密に減点し、閾値未満ならNeutralに落とす
                score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
        
        # 4. MACDクロス確認と減点 (15mのみ) - MACDヒストグラムの方向転換で代用
        if timeframe == '15m':
             # MACDヒストグラムがクロス直後に方向転換していないかチェック
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < df['MACDH_12_26_9'].iloc[-2]) or \
                                 (macd_hist_val < 0 and macd_hist_val > df['MACDH_12_26_9'].iloc[-2])
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(0.5, score - MACD_CROSS_PENALTY)
             else:
                 macd_valid = True

        # 5. TP/SLとRRRの決定 (ATRに基づく動的計算)
        
        # 長期トレンドと同じ方向の場合はRRRを高く設定
        rr_base = SHORT_TERM_BASE_RRR 
        if (timeframe != '4h') and (side == long_term_trend and long_term_trend != "Neutral"):
            rr_base = SHORT_TERM_MAX_RRR
        
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        tp_dist = sl_dist * rr_base 

        # 価格を修正
        if side == "ロング":
            entry = price 
            sl = entry - sl_dist
            tp1 = entry + tp_dist
        elif side == "ショート":
            entry = price 
            sl = entry + sl_dist
            tp1 = entry - tp_dist
        else:
            entry, sl, tp1, rr_base = price, 0, 0, 0
        
        # 6. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD or score < (1.0 - SIGNAL_THRESHOLD):
             final_side = "Neutral"

        # 7. tech_dataの構築
        bb_width_pct_val = (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / df['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df.columns else 0.0

        tech_data = {
            "rsi": rsi_val,
            "macd_hist": macd_hist_val, 
            "adx": adx_val,
            "bb_width_pct": bb_width_pct_val,
            "atr_value": atr_val,
            "long_term_trend": long_term_trend,
            "long_term_reversal_penalty": current_long_term_penalty_applied,
            "macd_cross_valid": macd_valid,
        }
        
    except Exception as e:
        # テクニカル分析失敗時のフォールバック処理
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中にエラーが発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults # 初期設定のデフォルト値を適用
        
    # 8. シグナル辞書を構築
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
        "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
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
    
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'])
    
    if status_4h == "Success" and len(df_4h) >= LONG_TERM_SMA_LENGTH:
        
        try:
            df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
        
            if not df_4h.empty and 'sma' in df_4h.columns and df_4h['sma'].iloc[-1] is not None:
                last_price = df_4h['close'].iloc[-1]
                last_sma = df_4h['sma'].iloc[-1]
                
                # 終値とSMAの相対位置でトレンドを決定
                if last_price > last_sma:
                    long_term_trend = 'Long'
                elif last_price < last_sma:
                    long_term_trend = 'Short'
        except Exception:
            pass # SMA計算エラーは無視
            
    # 1. 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend, False)
    ]
    
    results = await asyncio.gather(*tasks)
    
    # 4h分析結果の統合
    for result in results:
        if result and result.get('timeframe') == '4h':
            result.setdefault('tech_data', {})['long_term_trend'] = long_term_trend
    
    return [r for r in results if r is not None]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CCXT_CLIENT_NAME

    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            
            # ★ 出来高TOP30銘柄を動的に更新 (フォールバック強化)
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            macro_context = await get_crypto_macro_context()
            
            # ログ出力の改善 (表示用にOKXシンボル形式を標準形式に戻す)
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            # 各銘柄に対して統合シグナル生成タスクを実行
            tasks = [generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME) for symbol in monitor_symbols]
            
            # 全銘柄の分析を並行して実行
            results_list_of_lists = await asyncio.gather(*tasks)
            
            # 結果を平坦化
            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # -----------------------------------------------------------------
            # ★ 通知の選別ロジックと即時通知実行
            # -----------------------------------------------------------------
            
            # 銘柄ごとに、有効なシグナル（DataShortageやExchangeErrorではない）のみを抽出
            valid_signals = [s for s in all_signals if s.get('side') not in ["DataShortage", "ExchangeError"]]
            
            best_signals_per_symbol = {}
            for signal in valid_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                if signal.get('side') == 'Neutral':
                    continue

                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    # 関連する全時間軸のシグナルをまとめて格納
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals
                    }
            
            # スコアの高い順にソートし、閾値以上の上位N個を抽出
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: x['score'], 
                reverse=True
            )
            
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            # 通知実行
            if top_signals_to_notify:
                logging.info(f"🔔 高スコアシグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                notify_tasks = []
                for item in top_signals_to_notify:
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    # 1. クールダウンチェックを優先的に行う
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        # 2. メッセージを生成
                        msg = format_integrated_analysis_message(symbol, item['all_signals'])
                        
                        if msg:
                            # 3. 通知タスクをキューに追加
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (スコア: {item['score']:.4f})")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            
                            # asyncio.to_threadでI/O処理（requests.post）を直ちに別スレッドで実行
                            task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                            notify_tasks.append(task)
                            
                    else:
                        log_symbol = symbol.replace('-', '/')
                        logging.info(f"🕒 {log_symbol} はクールダウン期間中です。通知をスキップします。")
                
            # -----------------------------------------------------------------

            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            # 致命的なエラーログ
            logging.error(f"メインループで致命的なエラー: {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v12.0.0-DYNAMIC_LOGIC (Full Integrated)")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v12.0.0 Startup initializing...") 
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
        "bot_version": "v12.0.0-DYNAMIC_LOGIC (Full Integrated)",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v12.0.0, Full Integrated, Dynamic Logic)."}, status_code=200)

if __name__ == '__main__':
    # 実行環境に応じてポートとホストを調整してください
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
