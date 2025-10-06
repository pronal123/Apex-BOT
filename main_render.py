# ====================================================================================
# Apex BOT v12.1.21 - OKX Limit強制取得の試み
# 
# 修正点:
# - v12.1.20で判明した「OKX側でデータ取得が300本に制限される」問題への対処。
# - REQUIRED_OHLCV_LIMITSを400から500に増量。
# - fetch_ohlcvにparams={'limit': limit}を追加し、OKX APIへのリクエストLimitを強制。
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

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      # 出来高で選出する銘柄数
LOOP_INTERVAL = 360        # 6分間隔で分析を実行

# CCXT レート制限対策 
REQUEST_DELAY_PER_SYMBOL = 0.5 # 銘柄ごとのOHLCVリクエスト間の遅延 (秒)

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 2時間クールダウン
SIGNAL_THRESHOLD = 0.65             # 通知対象となる最低シグナル閾値 (0.5-1.0, 65点に相当)
TOP_SIGNAL_COUNT = 3                # 通知する上位銘柄数

# V12.1.21 FIX: OKXがリクエストLimitを無視する問題に対応するため、
# 1. リクエスト数を500に引き上げ、
# 2. CCXTの内部パラメータ(params)にもlimitを明示的に渡すことで強制取得を試みる。
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 # (KC導入によりこの定数は事実上不使用)

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.15   

MACD_CROSS_PENALTY = 0.08           
SHORT_TERM_BASE_RRR = 1.5           
SHORT_TERM_MAX_RRR = 2.5            
SHORT_TERM_SL_MULTIPLIER = 1.5      

# V12.1.12 FIX: Keltner Channel (KC) の定数
KC_LENGTH = 20
KC_MULTIPLIER = 2 
KC_FILTER_PENALTY = 0.04 # KCミドルラインと逆行する場合のペナルティ

# エントリーポジション最適化のための定数
ATR_PULLBACK_MULTIPLIER = 0.5 

# スコアリングロジック用の定数 
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 45 
RSI_MOMENTUM_HIGH = 55
ADX_TREND_THRESHOLD = 25
BASE_SCORE = 0.50  # 基本スコアを0.50に固定
VOLUME_BONUS = 0.03 # OBVによるボリューム確証ボーナス
MOMENTUM_CONFIRMATION_BONUS = 0.05 # CCI & Fisherによる強力なモメンタム確証ボーナス
MOMENTUM_CONTRARY_PENALTY = 0.05 # 逆行オシレーターによるペナルティ
VWAP_BONUS = 0.05 # VWAPによるトレンド一致ボーナス
VWAP_PENALTY = 0.05 # VWAPトレンド逆行ペナルティ

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING (変更なし)
# ====================================================================================

def convert_score_to_100(score: float) -> int:
    """0.5から1.0のスコアを50から100の点数に変換する"""
    score_pct = (score - 0.50) * 100.0
    return max(50, min(100, int(50 + score_pct)))

def get_signal_strength(score_100: int) -> Tuple[str, str]:
    """100点満点のスコアに基づきシグナル強度を分類し、ラベルとアイコンを返す"""
    if score_100 >= 85:
        return "強シグナル", "🔥🔥 強 (STRONG)"
    elif score_100 >= 75:
        return "中シグナル", "📈 中 (MEDIUM)"
    elif score_100 >= 65:
        return "弱シグナル", "🟡 弱 (WEAK)"
    else:
        return "N/A", "N/A"

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
    """スコアと時間軸に基づき推定勝率を算出する"""
    # 複数のオシレーターで確証されたシグナルは、スコアに対する勝率のゲインを強化
    adjusted_rate = 0.50 + (score - 0.50) * 0.7 
    return max(0.40, min(0.85, adjusted_rate))

def get_timeframe_eta(timeframe: str) -> str:
    """時間軸に基づきTP到達までの時間目安を返す"""
    if timeframe == '15m':
        return "数時間以内"
    elif timeframe == '1h':
        return "半日以内 (6〜12時間)"
    elif timeframe == '4h':
        return "1日〜数日 (24〜72時間)"
    return "N/A"

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、可視性を強化したメッセージを整形する。
    (v12.1.21: バージョン情報のみ更新)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    # 最高の取引シグナル（最もスコアが高く、かつSIGNAL_THRESHOLDを上回っているもの）を取得
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals:
        return "" 
        
    # スコアの小数点以下まで考慮してソート
    best_signal = sorted(high_score_signals, key=lambda s: s.get('score', 0.5), reverse=True)[0]
    
    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    score_100 = convert_score_to_100(score_raw) # 100点満点に変換
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    
    # OKX形式のシンボル (BTC-USDT) を標準形式 (BTC/USDT) に戻して表示
    display_symbol = symbol.replace('-', '/')

    # シグナル強度分類
    strength_label, strength_icon_text = get_signal_strength(score_100)
    
    # 順位アイコンの決定
    rank_icon = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
    eta_time = get_timeframe_eta(timeframe)

    # ----------------------------------------------------
    # 1. ヘッダーとスコアの可視化 (順位とシグナル強度を強調)
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    color_tag = "🟢" if side == "ロング" else "🔴"
    
    header = (
        f"--- {color_tag} --- **{display_symbol}** --- {color_tag} ---\n"
        f"**{rank_icon} 総合 {rank} 位！** {strength_icon_text} 発生！ - {direction_emoji}\n" 
        f"==================================\n"
        f"| 🥇 **分析スコア** | <b><u>{score_100} / 100 点</u></b> (ベース: {timeframe}足) |\n"
        f"| ⏰ **TP 到達目安** | <b>{eta_time}</b> | (RRR: 1:{rr_ratio:.2f}) |\n" 
        f"| 📈 **予測勝率** | <b>{get_estimated_win_rate(score_raw, timeframe) * 100:.1f}%</b> |\n"
        f"==================================\n"
    )

    # ----------------------------------------------------
    # 2. 推奨取引計画 (最適化エントリーとリスク距離を強調)
    # ----------------------------------------------------
    if entry_price > 0 and sl_price > 0:
        risk_dist = abs(entry_price - sl_price)
    else:
        risk_dist = 0.0

    trade_plan = (
        f"**🎯 推奨取引計画 (Keltner Channel最適化エントリー)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry (Limit)** | <code>${format_price_utility(entry_price, symbol)}</code> | **KCミドルラインへのプルバック** |\n"
        f"| 📉 **Risk (SL幅)** | <code>${format_price_utility(risk_dist, symbol)}</code> | 最小リスク距離 |\n" 
        f"| {color_tag} TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | 利確 (RRR: 1:{rr_ratio:.2f}) |\n"
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({SHORT_TERM_SL_MULTIPLIER:.1f}xATR) |\n"
        f"----------------------------------\n"
    )
    
    # ----------------------------------------------------
    # 3. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score_raw = s.get('score', 0.5)
        s_score_100 = convert_score_to_100(s_score_raw)
        tech_data = s.get('tech_data', {})
        
        if tf == '4h':
            long_trend = tech_data.get('long_term_trend', 'Neutral')
            analysis_detail += (
                f"🌏 **{tf} 足** (長期トレンド): **{long_trend}** ({s_score_100}点)\n"
            )
            
        else:
            score_icon = "🔥🔥" if s_score_100 >= 80 else ("📈" if s_score_100 >= 70 else "🟡" )
            penalty_status = " <i>(逆張りペナルティ適用)</i>" if tech_data.get('long_term_reversal_penalty') else ""
            
            # 強制Neutral化された場合の注釈
            if s_side == "Neutral" and convert_score_to_100(s_score_raw + LONG_TERM_REVERSAL_PENALTY) >= SIGNAL_THRESHOLD * 100: 
                 penalty_status += " <b>[⚠️ モメンタム反転により取消]</b>"
            
            # V12.1.7: モメンタム確証の詳細
            momentum_conf_count = tech_data.get('momentum_confirmation_count', 0)
            if momentum_conf_count > 0:
                 penalty_status += f" <b>[✅ モメンタム確証: {momentum_conf_count}点]</b>"
            
            # V12.1.8: VWAP確証の追加
            vwap_conf_status = tech_data.get('vwap_confirmation_status', 'Neutral')
            if vwap_conf_status == 'Confirmed':
                 penalty_status += " <b>[🌊 VWAP一致: OK]</b>"
            elif vwap_conf_status == 'Contradictory':
                 penalty_status += " <b>[❌ VWAP逆行: Penalty]</b>"
                 
            # V12.1.11: KCミドルライン確証の追加
            kc_filter_status = tech_data.get('kc_filter_status', 'Neutral')
            if kc_filter_status == 'Mid_Line_Contradictory':
                 penalty_status += " <b>[❌ KC逆行: Penalty]</b>"
            
            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({s_score_100}点) -> **{s_side}**{penalty_status}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({s.get('regime', 'N/A')})\n"
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

    # ----------------------------------------------------
    # 4. リスク管理とフッター
    # ----------------------------------------------------
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | v12.1.21 - OKX Limit強制取得の試み |\n" # <-- バージョン変更
        f"==================================\n"
        f"\n<pre>※ このシグナルは高度なテクニカル分析に基づきますが、投資判断は自己責任でお願いします。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (OKX)"""
    global EXCHANGE_CLIENT
    
    EXCHANGE_CLIENT = ccxt_async.okx({
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'} 
    })
    
    if EXCHANGE_CLIENT:
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - リアル接続, Default: Swap)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


def convert_symbol_to_okx_swap(symbol: str) -> str:
    """USDT現物シンボル (BTC/USDT) をOKXの無期限スワップシンボル (BTC-USDT) に変換する"""
    return symbol.replace('/', '-')


async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが未初期化のため、出来高による銘柄更新をスキップします。")
        return

    try:
        tickers_spot = await EXCHANGE_CLIENT.fetch_tickers(params={'instType': 'SPOT'})
        
        usdt_tickers = {
            symbol: ticker for symbol, ticker in tickers_spot.items() 
            if symbol.endswith('/USDT') and ticker.get('quoteVolume') is not None
        }

        sorted_tickers = sorted(
            usdt_tickers.items(), 
            key=lambda item: item[1]['quoteVolume'], 
            reverse=True
        )
        
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
            logging.info(f"✅ 出来高TOP{TOP_SYMBOL_LIMIT}銘柄をOKXスワップ形式に更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
        else:
            CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
            logging.warning("⚠️ 出来高データを取得できませんでした。前回成功したリストを使用します。")

    except Exception as e:
        logging.error(f"出来高による銘柄更新中にエラーが発生しました: {e}")
        CURRENT_MONITOR_SYMBOLS = LAST_SUCCESSFUL_MONITOR_SYMBOLS
        logging.warning("⚠️ 出来高データ取得エラー。前回成功したリストにフォールバックします。")

        
async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """CCXTを使用してOHLCVデータを取得し、エラー発生時にフォールバックする"""
    global EXCHANGE_CLIENT

    if not EXCHANGE_CLIENT:
        logging.error("CCXTクライアントが初期化されていません。")
        return [], "ExchangeError", client_name

    try:
        # V12.1.21: 500本を取得する設定
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        
        # V12.1.20/V12.1.21: 実際に使われるLIMIT値をログ出力
        logging.info(f"⚙️ {symbol} ({timeframe}): CCXTにリクエストするLimit値: {limit} 本") 
        
        # V12.1.21 NEW: CCXTにparams={'limit': limit}も明示的に渡す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol, 
            timeframe, 
            limit=limit, 
            params={'limit': limit} # OKXがリクエストLimitを無視する問題への対策
        )
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        # 取得したデータ本数をログ出力
        logging.info(f"✅ {symbol} ({timeframe}): OHLCVデータ {len(ohlcv)} 本を取得しました。")
            
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
# CORE ANALYSIS LOGIC (v12.1.21: 変更なし)
# ====================================================================================

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, four_hour_trend_context: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v12.1.21: 変更なし)
    """
    
    # 1. データ取得 
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "kc_width_pct": 0.0, "atr_value": 0.005, 
        "cci": 0.0, "fisher_transform": 0.0, "momentum_confirmation_count": 0, "vwap_confirmation_status": "Neutral",
        "kc_filter_status": "Neutral",
        "long_term_trend": four_hour_trend_context, "long_term_reversal_penalty": False, "macd_cross_valid": False,
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": BASE_SCORE, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # === V12.1.9/V12.1.10 FIX: pandas_taのVWAP計算に必要なDatetimeIndexを設定 ===
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms') 
    df.set_index('timestamp', inplace=True)
    # =================================================================
    
    df['close'] = pd.to_numeric(df['close'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['volume'] = pd.to_numeric(df['volume'])
    
    price = df['close'].iloc[-1] if not df.empty else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 

    final_side = "Neutral"
    base_score_candidate = BASE_SCORE
    macd_valid = False
    current_long_term_penalty_applied = False
    MACD_HIST_COL = 'MACD_Hist' 
    final_side_override = None 
    vwap_conf_status = "Neutral"
    kc_filter_status = "Neutral"

    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        
        # MACD
        df['EMA_12'] = ta.ema(df['close'], length=12)
        df['EMA_26'] = ta.ema(df['close'], length=26)
        df['MACD_Line'] = df['EMA_12'] - df['EMA_26']
        df['MACD_Signal'] = ta.ema(df['MACD_Line'], length=9)
        df[MACD_HIST_COL] = df['MACD_Line'] - df['MACD_Signal']
        
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        
        # V12.1.11 NEW: Keltner Channel (KC) の計算 
        df.ta.kc(length=KC_LENGTH, scalar=KC_MULTIPLIER, append=True)
        
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['obv'] = ta.obv(df['close'], df['volume'])
        
        # V12.1.7: CCIとFisher Transform
        df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20, c=0.015)
        df['fisher_transform'] = ta.fisher(df['high'], df['low'], length=9)['FISHERT_9_1']
        
        # V12.1.8: VWAPの追加
        vwap_series = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        
        # V12.1.10: 確実なPandas Seriesであることを確認する安全チェック
        if isinstance(vwap_series, pd.Series) and vwap_series.name and vwap_series.name.startswith('VWAP'):
             df['vwap'] = vwap_series 
        else:
             # VWAPの計算に失敗した場合
             df['vwap'] = np.nan 

        
        rsi_val = df['rsi'].iloc[-1]
        
        if df[MACD_HIST_COL].empty or pd.isna(df[MACD_HIST_COL].iloc[-1]) or pd.isna(df[MACD_HIST_COL].iloc[-2]):
            raise ValueError(f"{timeframe}のMACD計算結果がNaNまたはデータ不足です。")
            
        macd_hist_val = df[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df[MACD_HIST_COL].iloc[-2] 
        
        adx_val = df['adx'].iloc[-1]
        atr_val = df['atr'].iloc[-1] if not pd.isna(df['atr'].iloc[-1]) else atr_val

        obv_val = df['obv'].iloc[-1]
        obv_val_prev = df['obv'].iloc[-2]
        
        cci_val = df['cci'].iloc[-1]
        fisher_val = df['fisher_transform'].iloc[-1]
        
        vwap_val = df['vwap'].iloc[-1] if 'vwap' in df.columns and not pd.isna(df['vwap'].iloc[-1]) else None
        
        # =========================================================================
        # V12.1.19: KC指標値の抽出とデバッグ強化 (変更なし)
        # =========================================================================
        kc_upper_name = f'KCU_{KC_LENGTH}_{KC_MULTIPLIER}' # 例: KCU_20_2
        kc_lower_name = f'KCL_{KC_LENGTH}_{KC_MULTIPLIER}' # 例: KCL_20_2
        kc_mid_name = f'KCM_{KC_LENGTH}_{KC_MULTIPLIER}'   # 例: KCM_20_2

        kc_upper, kc_lower, kc_mid = 0, 0, 0
        kc_data_is_valid = False
        
        if kc_upper_name in df.columns:
            # 最後に有効なKC値を見つける (全バーをチェック)
            # i=1: 最新足, i=i: i-1本前の足
            for i in range(1, len(df) + 1): 
                # データフレームの長さがi以上であり、かつi番目のインデックスの値がNaNでないことを確認
                if not pd.isna(df[kc_upper_name].iloc[-i]):
                    kc_upper = df[kc_upper_name].iloc[-i]
                    kc_lower = df[kc_lower_name].iloc[-i]
                    kc_mid = df[kc_mid_name].iloc[-i]
                    kc_data_is_valid = True
                    
                    if i > 1:
                        # Log if we had to use an older bar
                        logging.warning(f"⚠️ {symbol} ({timeframe}) のKeltner Channelデータ ({kc_upper_name}) が最新足でNaNでした。{i-1}本前の有効な値を使用します。")
                    
                    # V12.1.19 NEW: KCデータが有効であることを確認する新しいログを追加
                    logging.info(f"✅ {symbol} ({timeframe}): Keltner Channelの有効な値 (バー: {len(df) - i}, 値: {kc_mid:.4f}) を発見し、使用します。")
                        
                    break 
                    
        if not kc_data_is_valid:
            # 全バーでNaNの場合、KCベースのロジックは無効
            logging.warning(f"⚠️ {symbol} ({timeframe}) のKeltner Channel列データがNaNまたは不足しています。代替処理を使用します。")
        # =========================================================================
        
        # ----------------------------------------------------
        # 2. 動的シグナル判断ロジック (Granular Scoring) (変更なし)
        # ----------------------------------------------------
        long_score = BASE_SCORE 
        short_score = BASE_SCORE 
        momentum_confirmation_count = 0
        
        # A. MACDに基づく方向性
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += 0.10 
            long_score += min(0.05, abs(macd_hist_val) * 10) 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += 0.10 
            short_score += min(0.05, abs(macd_hist_val) * 10)

        # B. RSIに基づく買われすぎ/売られすぎ
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.05
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.05
            
        # C. RSIに基づくモメンタムブレイクアウト
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.05
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.05

        # D. ADXに基づくトレンドフォロー強化
        if adx_val > ADX_TREND_THRESHOLD:
            adx_dynamic_bonus = max(0.0, adx_val - ADX_TREND_THRESHOLD) * 0.001 
            adx_total_bonus = 0.02 + adx_dynamic_bonus
            if long_score > short_score:
                long_score += adx_total_bonus
            elif short_score > long_score:
                short_score += adx_total_bonus

        # E. RSIの中立からの距離
        rsi_dist_bonus = abs(rsi_val - 50) * 0.0005 
        if rsi_val < 50:
            long_score += rsi_dist_bonus
        elif rsi_val > 50:
            short_score += rsi_dist_bonus

        # F. ボリューム確証 (OBV)
        if obv_val > obv_val_prev:
            long_score += VOLUME_BONUS
        elif obv_val < obv_val_prev:
            short_score += VOLUME_BONUS
            
        # G. CCI & Fisher Transformによるモメンタム確証
        cci_long_signal = cci_val > 100
        cci_short_signal = cci_val < -100
        fisher_long_signal = fisher_val > 0.5 
        fisher_short_signal = fisher_val < -0.5 
        
        if cci_long_signal and fisher_long_signal:
            long_score += MOMENTUM_CONFIRMATION_BONUS
            momentum_confirmation_count += 1
        elif cci_short_signal and fisher_short_signal:
            short_score += MOMENTUM_CONFIRMATION_BONUS
            momentum_confirmation_count += 1
        
        # 逆行ペナルティ (CCI/Fisher)
        if long_score > short_score:
            if cci_short_signal or fisher_short_signal:
                 long_score = max(BASE_SCORE, long_score - MOMENTUM_CONTRARY_PENALTY)
        elif short_score > long_score:
            if cci_long_signal or fisher_long_signal:
                 short_score = max(BASE_SCORE, short_score - MOMENTUM_CONTRARY_PENALTY)

        # H. VWAPによるトレンド確証
        if vwap_val is not None and vwap_val > 0:
            if price > vwap_val:
                if long_score > short_score:
                    long_score += VWAP_BONUS 
                    vwap_conf_status = "Confirmed"
                else:
                    short_score = max(BASE_SCORE, short_score - VWAP_PENALTY)
                    vwap_conf_status = "Contradictory"
            
            elif price < vwap_val:
                if short_score > long_score:
                    short_score += VWAP_BONUS
                    vwap_conf_status = "Confirmed"
                else:
                    long_score = max(BASE_SCORE, long_score - VWAP_PENALTY)
                    vwap_conf_status = "Contradictory"

        # I. Keltner Channelによるトレンド/レンジフィルタリング
        # kc_upper > 0 のチェックは、V12.1.17のデータ堅牢化ロジックによって保証される
        if kc_upper > 0: 
            if price > kc_upper: # 強力なロングトレンドブレイクアウト
                long_score += 0.05
                kc_filter_status = "Upper_Breakout"
            elif price < kc_lower: # 強力なショートトレンドブレイクアウト
                short_score += 0.05
                kc_filter_status = "Lower_Breakout"
            elif price > kc_mid: # KCミドルラインより上
                if short_score > long_score:
                    short_score = max(BASE_SCORE, short_score - KC_FILTER_PENALTY) # ミドルライン上でのショートペナルティ
                    kc_filter_status = "Mid_Line_Contradictory"
            elif price < kc_mid: # KCミドルラインより下
                if long_score > short_score:
                    long_score = max(BASE_SCORE, long_score - KC_FILTER_PENALTY) # ミドルライン下でのロングペナルティ
                    kc_filter_status = "Mid_Line_Contradictory"
        
        # 最終スコア方向の決定
        if long_score > short_score:
            side = "ロング"
            base_score_candidate = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score_candidate = short_score
        else:
            side = "Neutral"
            base_score_candidate = BASE_SCORE

        score = base_score_candidate
        
        # 3. 4hトレンドフィルターの適用 (変更なし)
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and four_hour_trend_context == "Short") or \
               (side == "ショート" and four_hour_trend_context == "Long"):
                score = max(BASE_SCORE, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
        
        # 4. MACDクロス確認と減点 + 強制Neutral化ロジック (変更なし)
        if timeframe == '15m':
             is_long_momentum_loss = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev)
             is_short_momentum_loss = (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)

             is_reversing_against_side = (side == "ロング" and is_long_momentum_loss) or \
                                         (side == "ショート" and is_short_momentum_loss)
             
             if is_reversing_against_side and score >= SIGNAL_THRESHOLD:
                 score = max(BASE_SCORE, score - MACD_CROSS_PENALTY)
                 final_side_override = "Neutral"
             else:
                 macd_valid = True

        # 5. TP/SLとRRRの決定 (KCミドルラインに基づくエントリー最適化)
        rr_base_ratio = SHORT_TERM_BASE_RRR
        
        # 1. ADXによるボラティリティ/トレンドに基づくRRRの基本値決定
        adx_val_capped = min(adx_val, 50.0)
        adx_normalized = max(0.0, (adx_val_capped - ADX_TREND_THRESHOLD) / (50.0 - ADX_TREND_THRESHOLD)) 
        
        if adx_normalized > 0:
            rr_base_ratio = SHORT_TERM_BASE_RRR + adx_normalized * (SHORT_TERM_MAX_RRR - SHORT_TERM_BASE_RRR)
        
        # 2. 長期トレンドとの一致による追加ブースト
        if (timeframe != '4h') and (side == four_hour_trend_context and four_hour_trend_context != "Neutral"):
            rr_base_ratio = min(SHORT_TERM_MAX_RRR + 0.5, rr_base_ratio + 0.5) 
            
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        tp_dist = sl_dist * rr_base_ratio 
        
        # エントリー価格をKCミドルライン付近に最適化 (プルバックを利用)
        if kc_mid is not None and kc_mid > 0:
            if side == "ロング":
                # エントリーは現在価格とKCミドルラインの中間、またはKCミドルラインに設定 (プルバック狙い)
                entry = max(price * 0.99, (price * 0.5) + (kc_mid * 0.5)) if price > kc_mid else price
            elif side == "ショート":
                # エントリーは現在価格とKCミドルラインの中間、またはKCミドルラインに設定 (戻り売り狙い)
                entry = min(price * 1.01, (price * 0.5) + (kc_mid * 0.5)) if price < kc_mid else price
            else:
                entry = price
        else:
            # KCが使えない場合、既存のATRプルバックロジックを使用
            pullback_dist = atr_val * ATR_PULLBACK_MULTIPLIER 
            if side == "ロング":
                entry = price - pullback_dist 
            elif side == "ショート":
                entry = price + pullback_dist
            else:
                entry = price

        # SL/TPの再計算（新しいエントリー価格を使用）
        if side == "ロング":
            sl = entry - sl_dist
            tp1 = entry + tp_dist
        elif side == "ショート":
            sl = entry + sl_dist
            tp1 = entry - tp_dist
        else:
            entry, tp1, sl, rr_base_ratio = price, 0, 0, 0
        
        # 6. 最終的なサイドの決定
        final_side = side
        
        if final_side_override is not None:
             final_side = final_side_override
        elif score < SIGNAL_THRESHOLD:
             final_side = "Neutral"

        # 7. RRRの計算 (最終的なRRR)
        rr_final = 0.0
        if final_side != "Neutral" and sl > 0 and tp1 > 0:
            if final_side == "ロング":
                risk = entry - sl
                reward = tp1 - entry
            else: 
                risk = sl - entry
                reward = entry - tp1
            
            if risk > 0 and reward > 0:
                 rr_final = round(reward / risk, 4) 
                 rr_final = min(SHORT_TERM_MAX_RRR * 2, rr_final) 
        
        # 8. RRRに基づく最終的な微調整スコア 
        if final_side != "Neutral" and rr_final > 0:
            score += rr_final * 0.005

        # 9. tech_dataの構築
        kc_channel_width_pct = (kc_upper - kc_lower) / df['close'].iloc[-1] * 100 if kc_upper > 0 and kc_lower > 0 else 0.0

        tech_data = {
            "rsi": rsi_val,
            "macd_hist": macd_hist_val, 
            "adx": adx_val,
            "kc_width_pct": kc_channel_width_pct,
            "atr_value": atr_val,
            "cci": cci_val,
            "fisher_transform": fisher_val,
            "vwap": vwap_val,
            "momentum_confirmation_count": momentum_confirmation_count, 
            "vwap_confirmation_status": vwap_conf_status,
            "kc_filter_status": kc_filter_status,
            "long_term_trend": four_hour_trend_context, 
            "long_term_reversal_penalty": current_long_term_penalty_applied,
            "macd_cross_valid": macd_valid,
        }
        
    except ValueError as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中にデータエラーが発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = BASE_SCORE
        entry, tp1, sl, rr_final = price, 0, 0, 0 
        tech_data = tech_data_defaults 

    except Exception as e:
        # 予期せぬエラー発生時
        logging.error(f"❌ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e}. Neutralとして処理を継続します。", exc_info=True)
        final_side = "Neutral"
        score = BASE_SCORE
        entry, tp1, sl, rr_final = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        
    # 10. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "side": final_side,
        "score": score, 
        "confidence": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_final if final_side != "Neutral" else 0.0,
        "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
        "macro_context": macro_context,
        "client": client_used,
        "timeframe": timeframe,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['kc_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD, 
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    """3つの時間軸のシグナルを統合して生成する (長期トレンド計算部分にIndex設定)"""
    
    # 0. 4hトレンドの事前計算 - 最初に必ず初期化する (変更なし)
    four_hour_trend_context = 'Neutral' 
    
    try:
        
        # 4h足のOHLCVを取得
        ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
        
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if status_4h == "Success" and len(df_4h) >= LONG_TERM_SMA_LENGTH:
            
            # === V12.1.9/V12.1.10 FIX: 4h足の長期トレンド計算にもDatetimeIndexを設定 ===
            df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms')
            df_4h.set_index('timestamp', inplace=True)
            # =================================================================
            
            df_4h['close'] = pd.to_numeric(df_4h['close'])
            
            try:
                df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
            
                if not df_4h.empty and 'sma' in df_4h.columns and df_4h['sma'].iloc[-1] is not None:
                    last_price = df_4h['close'].iloc[-1]
                    last_sma = df_4h['sma'].iloc[-1]
                    
                    if last_price > last_sma:
                        four_hour_trend_context = 'Long'
                    elif last_price < last_sma:
                        four_hour_trend_context = 'Short'
            except Exception as e:
                logging.warning(f"⚠️ {symbol} 4hトレンド計算エラー: {e}。Neutralトレンドとして続行します。")
                
        # 1. 各時間軸の分析を並行して実行
        tasks = [
            analyze_single_timeframe(symbol, '15m', macro_context, client_name, four_hour_trend_context, False),
            analyze_single_timeframe(symbol, '1h', macro_context, client_name, four_hour_trend_context, False),
            analyze_single_timeframe(symbol, '4h', macro_context, client_name, four_hour_trend_context, False) 
        ]
        
        results = await asyncio.gather(*tasks)
        
        # 4h分析結果の統合
        for result in results:
            if result and result.get('timeframe') == '4h':
                result.setdefault('tech_data', {})['long_term_trend'] = four_hour_trend_context
        
        return [r for r in results if r is not None]

    except Exception as e:
        logging.error(f"Generate Integrated Signalで予期せぬエラー: {symbol}: {e}")
        return [
            {"symbol": symbol, "side": "ExchangeError", "timeframe": tf, "score": BASE_SCORE, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "tech_data": {'long_term_trend': 'Neutral'}}
            for tf in ['15m', '1h', '4h']
        ]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP (v12.1.21: 変更なし)
# ====================================================================================

async def notify_integrated_analysis(symbol: str, signals: List[Dict], rank: int):
    """統合分析レポートをTelegramに送信"""
    global TRADE_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    if any(s.get('score', BASE_SCORE) >= SIGNAL_THRESHOLD and s.get('side') != "Neutral" for s in signals):
        if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
            
            msg = format_integrated_analysis_message(symbol, signals, rank) 
            
            if msg:
                log_symbol = symbol.replace('-', '/')
                max_score_100 = convert_score_to_100(max(s['score'] for s in signals))
                
                strength_label, _ = get_signal_strength(max_score_100)
                logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {rank}位, スコア: {max_score_100} 点, 強度: {strength_label})")
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                
                task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                return task
    return None

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CCXT_CLIENT_NAME

    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            macro_context = await get_crypto_macro_context()
            
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            results_list_of_lists = []
            notify_tasks = [] 
            
            for symbol in monitor_symbols:
                result = await generate_integrated_signal(symbol, macro_context, CCXT_CLIENT_NAME)
                results_list_of_lists.append(result)
                
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            valid_signals = [s for s in all_signals if s.get('side') not in ["DataShortage", "ExchangeError"]]
            
            best_signals_per_symbol = {}
            for signal in valid_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                if signal.get('side') == 'Neutral':
                    continue

                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals
                    }
            
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: x['score'], 
                reverse=True
            )
            
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            for rank, item in enumerate(top_signals_to_notify, 1):
                task = await notify_integrated_analysis(item['all_signals'][0]['symbol'], item['all_signals'], rank)
                if task:
                    notify_tasks.append(task)
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            if notify_tasks:
                 await asyncio.gather(*notify_tasks, return_exceptions=True)

            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            logging.error(f"メインループで致命的なエラー: {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v12.1.21-OKX_Limit_Force (Full Integrated)") # <-- バージョン変更

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v12.1.21 Startup initializing...") # <-- バージョン変更
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
        "bot_version": "v12.1.21-OKX_Limit_Force (Full Integrated)", # <-- バージョン変更
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v12.1.21, Full Integrated, OKX Limit Force)."}, status_code=200) # <-- バージョン変更

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
