# ====================================================================================
# Apex BOT v11.9.1 - クラッシュ修復＆シンボル互換性向上版
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

# ... (CONFIG & CONSTANTS, UTILITIES & FORMATTING は変更なし) ...

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
LOOP_INTERVAL = 360        
SYMBOL_WAIT = 0.0          

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

STRONG_NEUTRAL_MIN_DIFF = 0.02      
LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.15   

MACD_CROSS_PENALTY = 0.08           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.0            
SHORT_TERM_SL_MULTIPLIER = 1.0      


# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
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


def format_integrated_analysis_message(symbol: str, signals: List[Dict]) -> str:
    """
    3つの時間軸の分析結果を統合し、簡潔で見やすいTelegram通知メッセージを整形 
    **v11.9.1修正**: signalsリストが空でないことを保証する（ただし、呼び出し元でチェックされているはず）
    """
    
    # 🚨 致命的なエラー修正のため、呼び出し元からのリストが空でないことを確認
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    # 有効なシグナルがない場合は空文字列を返す (メインループのクラッシュ防止)
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
    
    # **v11.9.1修正**: tech_dataが存在しない場合の安全なアクセス
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
        elif s_score >= 0.65:
            analysis_parts.append(f"📈 {tf} ({s_score:.2f}): **{s_side}**")
        elif s_score <= 0.45:
            analysis_parts.append(f"📉 {tf} ({s_score:.2f}): **{s_side}**")
        else:
            analysis_parts.append(f"⚖️ {tf} ({s_score:.2f}): {s_side}")
            
    analysis_summary = " / ".join(analysis_parts)
    
    # メッセージ本体の構築
    header = (
        f"🎯 <b>高確度取引シグナル ({side})</b> 📊\n"
        f"---------------------------------------\n"
        f"| 銘柄: <b>{symbol}</b> | 時間軸: {timeframe} | スコア: <b>{score:.4f}</b> |\n"
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
    base, quote = symbol.split('/')
    return f"{base}-{quote}" 


async def update_symbols_by_volume():
    """
    CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する (v11.9.1: 現物出来高を取得し、スワップ形式に変換)
    """
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが未初期化のため、出来高による銘柄更新をスキップします。")
        return

    try:
        # 1. 現物市場のティッカーを取得 (出来高確認のため)
        # 一時的にdefaultTypeをspotに上書きしてティッカーを取得
        
        # v11.9.1 出来高取得エラー対応: fetch_tickersにparamsを指定してspotを取得
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        # 2. USDTペアをフィルタリングし、出来高順にソート (現物出来高: quoteVolume/volume_quote)
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
        
        # 3. 上位TOP_SYMBOL_LIMIT個を選出し、スワップシンボル形式に変換
        # 例: BTC/USDT -> BTC-USDT
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            logging.info(f"✅ 出来高TOP30銘柄をOKXスワップ形式に更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
        else:
            logging.warning("⚠️ 出来高データを取得できませんでした。前回またはデフォルトの銘柄リストを使用します。")

    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        # エラー時は前回リストを保持
        
        
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
        
        # 実際のOHLCVデータを取得 (CCXTクライアントのdefaultType='swap'に従う)
        # symbolはBTC-USDTのような形式で渡されることを想定
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        # ネットワークエラーはログに記録するが、クラッシュはさせない
        logging.warning(f"CCXT Network Error ({symbol} {timeframe}): {EXCHANGE_CLIENT.url}{e}")
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        # 銘柄が見つからないエラーはログに記録し、クラッシュはさせない
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
# CORE ANALYSIS LOGIC (時間軸ごとの分離)
# ====================================================================================

# ... (analyze_single_timeframe 関数は変更なし) ...
async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    # 🚨 v11.9.1 修正: DataShortage/ExchangeErrorの場合でも、tech_dataを空にしないように初期化を保持
    # これにより、generate_integrated_signalがアクセスした際にKeyErrorを防ぐ
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
    }
    
    if status != "Success":
        # エラー時もtech_dataを含む辞書を返すように修正
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    
    # デフォルト値の初期設定 
    price = df['close'].iloc[-1] if not df.empty else 0.0
        
    final_side = "Neutral"
    score = 0.5
    entry, tp1, sl, rr_base = 0, 0, 0, 0
    atr_val = price * 0.005 if price > 0 else 0.005 
    macd_valid = False
    current_long_term_penalty_applied = False
    
    tech_data = tech_data_defaults 

    # ----------------------------------------------------
    # 🚨 危険な操作ブロック (テクニカル指標の計算と結果へのアクセス) 
    # ----------------------------------------------------
    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        df.ta.macd(close='close', fast=12, slow=26, signal=9, append=True)
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True)
        # ATRを計算
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        atr_val = df['atr'].iloc[-1] if 'atr' in df.columns and df['atr'].iloc[-1] is not None else atr_val
        
        # 2. 基本シグナル判断ロジック（RSIに基づくダミー）
        rsi_val = df['rsi'].iloc[-1]
        if rsi_val > 70:
            side = "ショート"
            base_score = 0.70 + random.uniform(0.01, 0.15) 
        elif rsi_val < 30:
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
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(0.5, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
        
        # 4. MACDクロス確認と減点 (15mのみ)
        required_macd_cols = ['MACD_12_26_9', 'MACDS_12_26_9', 'MACDH_12_26_9']
        
        if all(col in df.columns for col in required_macd_cols) and len(df) >= 2:
            
            macd_line = df['MACD_12_26_9']
            signal_line = df['MACDS_12_26_9']
            
            if timeframe == '15m':
                is_long_cross = (macd_line.iloc[-2] < signal_line.iloc[-2]) and (macd_line.iloc[-1] >= signal_line.iloc[-1])
                is_short_cross = (macd_line.iloc[-2] > signal_line.iloc[-2]) and (macd_line.iloc[-1] <= signal_line.iloc[-1])
                
                if (side == "ロング" and is_long_cross) or (side == "ショート" and is_short_cross):
                    macd_valid = True
            
        if not macd_valid and score >= SIGNAL_THRESHOLD and timeframe == '15m':
            score = max(0.5, score - MACD_CROSS_PENALTY)
                
        # 5. TP/SLとRRRの決定 
        rr_base = SHORT_TERM_BASE_RRR 
        if (timeframe != '4h') and (side == long_term_trend):
            rr_base = SHORT_TERM_MAX_RRR
        
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        tp_dist = sl_dist * rr_base 

        # 価格を修正: Entryは現在の価格、TP/SLは価格から距離を計算
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
        if score < SIGNAL_THRESHOLD and score > (1.0 - SIGNAL_THRESHOLD):
             if abs(score - 0.5) < STRONG_NEUTRAL_MIN_DIFF: 
                 final_side = "Neutral"
        elif score < (1.0 - SIGNAL_THRESHOLD): 
             final_side = "Neutral"

        # 7. tech_dataの構築 (計算成功時の値を使用)
        macd_hist_val = df['MACDH_12_26_9'].iloc[-1] if 'MACDH_12_26_9' in df.columns else 0.0
        adx_val = df['adx'].iloc[-1] if 'adx' in df.columns else 25.0
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
        "regime": "トレンド" if tech_data['adx'] >= 25 else "レンジ",
        "macro_context": macro_context,
        "client": client_used,
        "timeframe": timeframe,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
    }
    
    return signal_candidate
# ... (analyze_single_timeframe 関数はここまで) ...

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
        
        # SMA計算もエラーハンドリングの対象外なので安全にチェック
        try:
            df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
        
            if not df_4h.empty and 'sma' in df_4h.columns and df_4h['sma'].iloc[-1] is not None:
                last_price = df_4h['close'].iloc[-1]
                last_sma = df_4h['sma'].iloc[-1]
                
                if last_price > last_sma:
                    long_term_trend = 'Long'
                elif last_price < last_sma:
                    long_term_trend = 'Short'
        except Exception:
            pass # SMA計算エラーは無視し、Neutralのままにする
            
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
            # 🚨 v11.9.1 修正: tech_dataが存在しない可能性があるため、getでアクセスし、デフォルト値を設定
            result.get('tech_data', {})['long_term_trend'] = long_term_trend
    
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
            
            # 出来高TOP30銘柄を動的に更新
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            macro_context = await get_crypto_macro_context()
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})")
            
            # 各銘柄に対して統合シグナル生成タスクを実行
            tasks = [generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME) for symbol in monitor_symbols]
            
            # 全銘柄の分析を並行して実行
            results_list_of_lists = await asyncio.gather(*tasks)
            
            # 結果を平坦化
            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] # エラーシグナルも含める
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # -----------------------------------------------------------------
            # ★ 通知の選別ロジック (最もスコアの高いシグナルを基準に選出)
            # -----------------------------------------------------------------
            
            # 銘柄ごとに、有効なシグナル（DataShortageやExchangeErrorではない）のみを抽出
            valid_signals = [s for s in all_signals if s.get('side') not in ["DataShortage", "ExchangeError"]]
            
            best_signals_per_symbol = {}
            for signal in valid_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                # Neutralではないシグナルのみを対象とする
                if signal.get('side') == 'Neutral':
                    continue

                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    # 関連する全時間軸のシグナルをまとめて格納 (ここではエラーシグナルも含めて渡す)
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
                logging.info(f"🔔 高スコアシグナル {len(top_signals_to_notify)} 銘柄を通知します。")
                for item in top_signals_to_notify:
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    # クールダウンチェック (銘柄ごとに2時間)
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        msg = format_integrated_analysis_message(symbol, item['all_signals'])
                        if msg:
                            # ログ表示用に、OKXシンボル形式を標準形式に戻す (BTC-USDT -> BTC/USDT)
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知送信: {log_symbol} (スコア: {item['score']:.4f})")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(msg)))
                        
            # -----------------------------------------------------------------

            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            # 🚨 v11.9.1 修正: 致命的なエラーログの改善。特にKeyError: 'tech_data'を防止
            logging.error(f"メインループで致命的なエラー: {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v11.9.1-CRASH_FIX_OKX_SWAP (Full Integrated)")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v11.9.1 Startup initializing...") 
    # バックグラウンドでメインループを実行
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        # シャットダウン時にCCXTクライアントをクローズ
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v11.9.1-CRASH_FIX_OKX_SWAP (Full Integrated)",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v11.9.1, Full Integrated, Crash Fix, OKX Swap)."}, status_code=200)

if __name__ == '__main__':
    # 実行環境に応じてポートとホストを調整してください
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
