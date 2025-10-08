# ====================================================================================
# Apex BOT v14.0.0 - Strict High-Conviction Logic & Enhanced Win Rate Focus
# - スコア条件を厳密化 (SIGNAL_THRESHOLDを0.75、BASE_SCOREを0.40に調整)
# - ペナルティ/確証要素の重みを強化し、高スコアを出すための条件を厳格化
# - 動的なSL/TP、構造的分析、FGI Proxy統合はv13.0.0から維持
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
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             # 閾値を 0.70 -> 0.75 に引き上げ (厳格化)
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   # ペナルティを 0.15 -> 0.20 に強化
MACD_CROSS_PENALTY = 0.15           # ペナルティを 0.10 -> 0.15 に強化
SHORT_TERM_MIN_RRR = 1.5           
SHORT_TERM_MAX_RRR = 3.0            
SHORT_TERM_SL_MULTIPLIER = 1.5      
ATR_TP_MIN_MULTIPLIER = 1.5         

# スコアリングロジック用の定数 (v14.0.0 変更点)
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               # 45 -> 40 に変更 (より強いモメンタムを要求)
RSI_MOMENTUM_HIGH = 60              # 55 -> 60 に変更 (より強いモメンタムを要求)
ADX_TREND_THRESHOLD = 30            # 25 -> 30 に変更 (強いトレンドのみボーナス)
BASE_SCORE = 0.40                   # 0.50 -> 0.40 に変更 (高スコアをより困難に)
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 # 2.0 -> 2.5 に変更 (出来高急増の閾値強化)

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'OKX' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = [s.replace('/', '-') for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}

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

def get_tp_reach_time(timeframe: str) -> str:
    """時間足に基づきTP到達目安を算出する (ログメッセージ用)"""
    if timeframe == '15m': return "数時間以内 (2〜4時間)"
    if timeframe == '1h': return "半日以内 (6〜12時間)"
    if timeframe == '4h': return "数日以内 (1〜3日)"
    return "N/A"

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
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    # 0.75を基準に、1.00で最大の85%に近づくよう調整 (より厳しい傾斜を設定)
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    3つの時間軸の分析結果を統合し、ログメッセージの形式に整形する (v14.0.0対応)
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    
    if not valid_signals:
        return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    
    if not high_score_signals:
        return "" 
        
    best_signal = max(
        high_score_signals, 
        key=lambda s: (
            s.get('score', 0.5), 
            s.get('rr_ratio', 0.0), 
            s.get('tech_data', {}).get('adx', 0.0), 
            -s.get('tech_data', {}).get('atr_value', 1.0),
            s.get('symbol', '')
        )
    )
    
    # 主要な取引情報を抽出
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A').upper()
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    tp_price = best_signal.get('tp1', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    entry_type = best_signal.get('entry_type', 'N/A') 

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    display_symbol = symbol.replace('-', '/')
    
    # リスク幅を計算
    sl_width = abs(entry_price - sl_price)
    
    # ----------------------------------------------------
    # 1. ヘッダーとエントリー情報の可視化
    # ----------------------------------------------------
    direction_emoji = "🚀 **ロング (LONG)**" if side == "ロング" else "💥 **ショート (SHORT)**"
    strength = "極めて良好 (VERY HIGH)" if score_raw >= 0.85 else ("高 (HIGH)" if score_raw >= 0.75 else "中 (MEDIUM)")
    
    rank_header = ""
    if rank == 1: rank_header = "🥇 **総合 1 位！**"
    elif rank == 2: rank_header = "🥈 **総合 2 位！**"
    elif rank == 3: rank_header = "🥉 **総合 3 位！**"
    else: rank_header = f"🏆 **総合 {rank} 位！**"

    market_sentiment_str = ""
    macro_sentiment = best_signal.get('macro_context', {}).get('sentiment_fgi_proxy', 0.0)
    if macro_sentiment >= 0.05: # 0.03 -> 0.05 (厳格化された閾値)
         market_sentiment_str = " (リスクオン傾向)"
    elif macro_sentiment <= -0.05:
         market_sentiment_str = " (リスクオフ傾向)"
    
    header = (
        f"--- 🟢 --- **{display_symbol}** --- 🟢 ---\n"
        f"{rank_header} 📈 {strength} 発生！ - {direction_emoji}{market_sentiment_str}\n" 
        f"==================================\n"
        f"| 🎯 **予測勝率** | **<ins>{win_rate:.1f}%</ins>** | **条件極めて良好** |\n"
        f"| 💯 **分析スコア** | <b>{score_100:.2f} / 100.00 点</b> (ベース: {timeframe}足) |\n" 
        f"| ⏰ **TP 到達目安** | {get_tp_reach_time(timeframe)} | (RRR: 1:{rr_ratio:.2f}) |\n"
        f"==================================\n"
    )

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (DC/Pivot)"
        
    trade_plan = (
        f"**🎯 推奨取引計画 (Dynamic RRR & Structural SL)**\n"
        f"----------------------------------\n"
        f"| 指標 | 価格 (USD) | 備考 |\n"
        f"| :--- | :--- | :--- |\n"
        f"| 💰 現在価格 | <code>${format_price_utility(price, symbol)}</code> | 参照価格 |\n"
        f"| ➡️ **Entry ({entry_type})** | <code>${format_price_utility(entry_price, symbol)}</code> | {side}ポジション ({entry_type}注文) |\n" 
        f"| 📉 **Risk (SL幅)** | ${format_price_utility(sl_width, symbol)} | 最小リスク距離 |\n"
        f"| 🟢 TP 目標 | <code>${format_price_utility(tp_price, symbol)}</code> | 利確 (RRR: 1:{rr_ratio:.2f}) |\n"
        f"| ❌ SL 位置 | <code>${format_price_utility(sl_price, symbol)}</code> | 損切 ({sl_source_str}) |\n"
        f"----------------------------------\n"
    )
    
    # ----------------------------------------------------
    # 2. 統合分析サマリーとスコアリングの詳細
    # ----------------------------------------------------
    analysis_detail = "**💡 統合シグナル生成の根拠 (3時間軸)**\n"
    
    long_term_trend_4h = 'Neutral'
    
    for s in signals:
        tf = s.get('timeframe')
        s_side = s.get('side', 'N/A')
        s_score = s.get('score', 0.5)
        tech_data = s.get('tech_data', {})
        
        score_in_100 = s_score * 100
        
        if tf == '4h':
            long_term_trend_4h = tech_data.get('long_term_trend', 'Neutral')
            
            analysis_detail += (
                f"🌏 **4h 足** (長期トレンド): **{long_term_trend_4h}** ({score_in_100:.2f}点)\n"
            )
            
        else:
            score_icon = "🔥" if s_score >= 0.75 else ("📈" if s_score >= 0.65 else "🟡" )
            
            penalty_status = f" (逆張りペナルティ: -{tech_data.get('long_term_reversal_penalty_value', 0.0) * 100:.1f}点適用)" if tech_data.get('long_term_reversal_penalty') else ""
            
            momentum_valid = tech_data.get('macd_cross_valid', True)
            momentum_text = "[✅ モメンタム確証: OK]" if momentum_valid else f"[⚠️ モメンタム反転により減点: -{tech_data.get('macd_cross_penalty_value', 0.0) * 100:.1f}点]"

            vwap_consistent = tech_data.get('vwap_consistent', False)
            vwap_text = "[🌊 VWAP一致: OK]" if vwap_consistent else "[🌊 VWAP不一致: NG]"

            stoch_penalty = tech_data.get('stoch_filter_penalty', 0.0)
            stoch_text = ""
            if stoch_penalty > 0:
                 stoch_text = f" [⚠️ STOCHRSI 過熱感により減点: -{stoch_penalty * 100:.2f}点]"
            elif stoch_penalty == 0 and tf in ['15m', '1h']:
                 stoch_text = f" [✅ STOCHRSI 確証]"

            analysis_detail += (
                f"**[{tf} 足] {score_icon}** ({score_in_100:.2f}点) -> **{s_side}**{penalty_status} {momentum_text} {vwap_text} {stoch_text}\n"
            )
            
            # 採用された時間軸の技術指標を詳細に表示
            if tf == timeframe:
                regime = best_signal.get('regime', 'N/A')
                # ADX/Regime
                analysis_detail += f"   └ **ADX/Regime**: {tech_data.get('adx', 0.0):.2f} ({regime})\n"
                # RSI/MACDH/CCI/STOCH
                analysis_detail += f"   └ **RSI/MACDH/CCI**: {tech_data.get('rsi', 0.0):.2f} / {tech_data.get('macd_hist', 0.0):.4f} / {tech_data.get('cci', 0.0):.2f}\n"

                # New: Structural/Pivot Analysis
                pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
                pivot_status = "✅ 構造的S/R確証" if pivot_bonus > 0 else "❌ 構造確証なし"
                analysis_detail += f"   └ **構造分析(Pivot)**: {pivot_status} (+{pivot_bonus * 100:.2f}点)\n"


                # 出来高確証の表示
                volume_bonus = tech_data.get('volume_confirmation_bonus', 0.0)
                if volume_bonus > 0:
                    analysis_detail += f"   └ **出来高/流動性確証**: ✅ +{volume_bonus * 100:.2f}点 ボーナス追加 (平均比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"
                else:
                    analysis_detail += f"   └ **出来高/流動性確証**: ❌ 確認なし (比率: {tech_data.get('volume_ratio', 0.0):.1f}x)\n"


    # 3. リスク管理とフッター
    regime = best_signal.get('regime', 'N/A')
    
    footer = (
        f"==================================\n"
        f"| 🔍 **市場環境** | **{regime}** 相場 (ADX: {best_signal.get('tech_data', {}).get('adx', 0.0):.2f}) |\n"
        f"| ⚙️ **BOT Ver** | v14.0.0 - Strict High-Conviction Logic |\n" 
        f"==================================\n"
        f"\n<pre>※ このシグナルは高度なテクニカル分析に基づきますが、投資判断は自己責任でお願いします。</pre>"
    )

    return header + trade_plan + analysis_detail + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================
# (データ取得ロジックはv13.0.0から変更なし)
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
            # logging.info(f"✅ 出来高TOP{TOP_SYMBOL_LIMIT}銘柄をOKXスワップ形式に更新しました。例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5])}...")
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
        # logging.error("CCXTクライアントが初期化されていません。")
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        # logging.warning(f"CCXT Network Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        if 'market symbol' in str(e) or 'not found' in str(e):
             spot_symbol = symbol.replace('-', '/')
             try:
                 ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
                 if not ohlcv or len(ohlcv) < 30: 
                     return [], "DataShortage", client_name
                 return ohlcv, "Success", client_name
             except Exception:
                 # logging.warning(f"CCXT Exchange Error (SPOT Fallback Failed - {symbol} {timeframe}): {e}")
                 return [], "ExchangeError", client_name
        
        # logging.warning(f"CCXT Exchange Error ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name
        
    except Exception as e:
        # logging.error(f"予期せぬデータ取得エラー ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """
    マクロ市場コンテキストを取得 (FGI Proxy & BTC/ETH Trend)
    """
    
    # 1. BTC/USDTとETH/USDTの長期トレンドとボラティリティを取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC-USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH-USDT", '4h')
    
    btc_trend = 0
    eth_trend = 0
    
    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH)
        df_btc.dropna(subset=['sma'], inplace=True)
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]: btc_trend = 1 # Long
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]: btc_trend = -1 # Short
    
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]: eth_trend = 1 # Long
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]: eth_trend = -1 # Short

    # 2. FGI Proxyの計算 (恐怖指数/市場センチメント)
    # 以下のロジックでセンチメントをスコア化 (Max: 0.05 -> 0.07, Min: -0.05 -> -0.07)
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = 0.07 # 強いリスクオン (ボーナス強化)
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -0.07 # 強いリスクオフ (恐怖) (ボーナス強化)
        
    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score
    }


# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

# Fibonacci Pivot Point Calculation Utility (Simplified)
def calculate_fib_pivot(df: pd.DataFrame) -> Dict:
    """直近のバーに基づきフィボナッチ・ピボットポイントを計算する (H, L, C, P, R, S)"""
    if len(df) < 1:
        return {'P': np.nan, 'R1': np.nan, 'S1': np.nan}
    
    # 終値 (C) と直近の H/L を取得 (最終バーの前のバーを使う方がより信頼できる構造を与える)
    H = df['high'].iloc[-2] if len(df) >= 2 else df['high'].iloc[-1]
    L = df['low'].iloc[-2] if len(df) >= 2 else df['low'].iloc[-1]
    C = df['close'].iloc[-1]
    
    P = (H + L + C) / 3
    
    # フィボナッチ係数
    R1 = P + (H - L) * 0.382
    S1 = P - (H - L) * 0.382
    
    return {'P': P, 'R1': R1, 'S1': S1}

def analyze_structural_proximity(price: float, pivots: Dict, side: str) -> Tuple[float, float, float]:
    """
    価格とPivotポイントを比較し、構造的なSL/TPを決定し、ボーナススコアを返す (0.05 -> 0.07点)
    返り値: (ボーナススコア, 構造的SL, 構造的TP)
    """
    bonus = 0.0
    structural_sl = 0.0
    structural_tp = 0.0
    BONUS_POINT = 0.07 # ボーナスを 0.07 に強化

    R1 = pivots.get('R1', np.nan)
    S1 = pivots.get('S1', np.nan)

    if pd.isna(R1) or pd.isna(S1):
        return 0.0, 0.0, 0.0

    # 構造的なSL/TPの採用
    if side == "ロング":
        # PriceがS1の上、かつS1に近い: 買いの確証 + SLをS1に設定 (よりタイトなSLを採用)
        if price > S1 and (price - S1) / (R1 - S1) < 0.5:
            bonus = BONUS_POINT # 構造的サポートの確証
            structural_sl = S1
            structural_tp = R1 * 1.01 
        # PriceがR1をブレイクした直後: Continuation
        elif price > R1:
            bonus = BONUS_POINT
            structural_sl = R1 # R1をSLに設定
            structural_tp = R1 * 1.05 
            
    elif side == "ショート":
        # PriceがR1の下、かつR1に近い: 売りの確証 + SLをR1に設定 (よりタイトなSLを採用)
        if price < R1 and (R1 - price) / (R1 - S1) < 0.5:
            bonus = BONUS_POINT # 構造的レジスタンスの確証
            structural_sl = R1
            structural_tp = S1 * 0.99 
        # PriceがS1をブレイクした直後: Continuation
        elif price < S1:
            bonus = BONUS_POINT
            structural_sl = S1 # S1をSLに設定
            structural_tp = S1 * 0.95 

    return bonus, structural_sl, structural_tp

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v14.0.0 - Strict Logic)
    """
    
    # 1. データ取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
        "cci": 0.0, "vwap_consistent": False, "ppo_hist": 0.0, "dc_high": 0.0, "dc_low": 0.0,
        "stoch_k": 50.0, "stoch_d": 50.0, "stoch_filter_penalty": 0.0,
        "volume_confirmation_bonus": 0.0, "current_volume": 0.0, "average_volume": 0.0,
        "sentiment_fgi_proxy_bonus": 0.0, "structural_pivot_bonus": 0.0, 
        "volume_ratio": 0.0, "structural_sl_used": False,
        "long_term_reversal_penalty_value": 0.0, # NEW for logging
        "macd_cross_penalty_value": 0.0 # NEW for logging
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    # 💡 データ型変換とインデックス設定の厳格化
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')

    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    
    price = df['close'].iloc[-1] if not df.empty and not pd.isna(df['close'].iloc[-1]) else 0.0
    atr_val = price * 0.005 if price > 0 else 0.005 

    final_side = "Neutral"
    base_score = BASE_SCORE # 0.40 からスタート (v14.0.0)
    macd_valid = False
    current_long_term_penalty_applied = False
    
    MACD_HIST_COL = 'MACD_Hist'     
    PPO_HIST_COL = 'PPOh_12_26_9'   
    STOCHRSI_K = 'STOCHRSIk_14_14_3_3'
    STOCHRSI_D = 'STOCHRSId_14_14_3_3'

    try:
        # テクニカル指標の計算
        df['rsi'] = ta.rsi(df['close'], length=14)
        
        df['EMA_12'] = ta.ema(df['close'], length=12)
        df['EMA_26'] = ta.ema(df['close'], length=26)
        df['MACD_Line'] = df['EMA_12'] - df['EMA_26']
        df['MACD_Signal'] = ta.ema(df['MACD_Line'], length=9)
        df[MACD_HIST_COL] = df['MACD_Line'] - df['MACD_Signal']
        
        df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
        df.ta.bbands(close='close', length=20, append=True) 
        df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['cci'] = ta.cci(df['high'], df['low'], df['close'], length=20)
        df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
        df.ta.ppo(append=True) 
        df.ta.donchian(length=20, append=True) 
        df.ta.stochrsi(append=True)
        
        # 💡 Pivot Pointの計算 (Structural Analysis)
        pivots = calculate_fib_pivot(df)
        
        # 💡 データクリーニング (NaN値の削除)
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', PPO_HIST_COL] 
        if STOCHRSI_K in df.columns: required_cols.append(STOCHRSI_K)
        if STOCHRSI_D in df.columns: required_cols.append(STOCHRSI_D)
        df.dropna(subset=required_cols, inplace=True)
        
        dc_cols_present = 'DCL_20' in df.columns and 'DCU_20' in df.columns
        
        if df.empty or len(df) < 2: 
            return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A"}

        # 2. **動的シグナル判断ロジック (スコアリング)**
        
        rsi_val = df['rsi'].iloc[-1]
        macd_hist_val = df[MACD_HIST_COL].iloc[-1] 
        macd_hist_val_prev = df[MACD_HIST_COL].iloc[-2] 
        adx_val = df['adx'].iloc[-1]
        atr_val = df['atr'].iloc[-1]
        cci_val = df['cci'].iloc[-1] 
        vwap_val = df['vwap'].iloc[-1] 
        ppo_hist_val = df[PPO_HIST_COL].iloc[-1] 
        stoch_k_val = df[STOCHRSI_K].iloc[-1] if STOCHRSI_K in df.columns else 50.0
        current_volume = df['volume'].iloc[-1]
        average_volume = df['volume'].iloc[-31:-1].mean() if len(df) >= 31 else df['volume'].mean()
        volume_ratio = current_volume / average_volume if average_volume > 0 else 0.0

        long_score = BASE_SCORE # 0.40
        short_score = BASE_SCORE # 0.40
        
        # Donchian Channelの値の安全な取得
        dc_low_val = price 
        dc_high_val = price

        if dc_cols_present:
            dc_low_val = df['DCL_20'].iloc[-1]     
            dc_high_val = df['DCU_20'].iloc[-1]
        
        # A. MACDに基づく方向性 (0.18 -> 0.15)
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += 0.15 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += 0.15 

        # B. RSIに基づく買われすぎ/売られすぎ (0.10 -> 0.08)
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.08
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.08
            
        # C. RSIに基づくモメンタムブレイクアウト (0.10 -> 0.12)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.12 # 閾値が60に上がったため、ボーナスを維持
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.12 # 閾値が40に下がったため、ボーナスを維持

        # D. ADXに基づくトレンドフォロー強化 (0.08 -> 0.10 / ADX閾値 25 -> 30)
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score:
                long_score += 0.10
            elif short_score > long_score:
                short_score += 0.10
        
        # E. VWAPの一致チェック (0.05 -> 0.04)
        vwap_consistent = False
        if price > vwap_val:
            long_score += 0.04
            vwap_consistent = True
        elif price < vwap_val:
            short_score += 0.04
            vwap_consistent = True
        
        # F. PPOに基づくモメンタム強度の評価 (0.05 -> 0.04)
        ppo_abs_mean = df[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean:
            long_score += 0.04 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean:
            short_score += 0.04

        # G. Donchian Channelによるブレイクアウト/過熱感フィルター (0.13 -> 0.15)
        is_breaking_high = False
        is_breaking_low = False
        if dc_cols_present: 
            is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
            is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val

            if is_breaking_high:
                long_score += 0.15 # 構造的なブレイクアウトの重要性を強化
            elif is_breaking_low:
                short_score += 0.15
        
        # H. 複合モメンタム加速ボーナス (0.07 -> 0.05)
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50:
             long_score += 0.05
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50:
             short_score += 0.05
        
        # 最終スコア決定 (中間)
        if long_score > short_score:
            side = "ロング"
            base_score = long_score
        elif short_score > long_score:
            side = "ショート"
            base_score = short_score
        else:
            side = "Neutral"
            base_score = BASE_SCORE
        
        score = min(1.0, base_score) 

        # ----------------------------------------------------------------------
        # K. NEW: 市場センチメント (FGI Proxy) の適用 (+/-0.07点)
        # ----------------------------------------------------------------------
        sentiment_bonus = macro_context.get('sentiment_fgi_proxy', 0.0) # 値は+/-0.07
        if side == "ロング" and sentiment_bonus > 0:
            score = min(1.0, score + sentiment_bonus)
        elif side == "ショート" and sentiment_bonus < 0:
            score = min(1.0, score + abs(sentiment_bonus))
        
        # ----------------------------------------------------------------------
        # L. NEW: Structural/Pivot Analysis (0.07点)
        # ----------------------------------------------------------------------
        structural_pivot_bonus, structural_sl_pivot, structural_tp_pivot = analyze_structural_proximity(price, pivots, side)
        score = min(1.0, score + structural_pivot_bonus)
        
        # ----------------------------------------------------------------------
        # J. 出来高/流動性確証 (Max 0.12)
        # ----------------------------------------------------------------------
        volume_confirmation_bonus = 0.0
        
        # 出来高が平均の2.5倍以上 (閾値強化)
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER: 
            
            # 流動性を伴うDCブレイクアウト (0.06点)
            if dc_cols_present and (is_breaking_high or is_breaking_low):
                volume_confirmation_bonus += 0.06
            
            # 流動性を伴う強力なMACDモメンタム (0.06点)
            if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean():
                volume_confirmation_bonus += 0.06
                
            score = min(1.0, score + volume_confirmation_bonus)
        
        
        # 3. 4hトレンドフィルターの適用 (15m, 1hのみ) 
        penalty_value_lt = 0.0
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(BASE_SCORE, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
                penalty_value_lt = LONG_TERM_REVERSAL_PENALTY
        
        # 4. MACDクロス確認と減点 (モメンタム反転チェック)
        macd_valid = True
        penalty_value_macd = 0.0
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(BASE_SCORE, score - MACD_CROSS_PENALTY)
                 macd_valid = False
                 penalty_value_macd = MACD_CROSS_PENALTY
             
        
        # 5. TP/SLとRRRの決定 (Dynamic RRR & Structural SL)
        
        rr_base = SHORT_TERM_MIN_RRR
        
        # RRRのスケールファクター (ADXが高いほど、または長期トレンド一致でRRRを高く設定)
        adx_factor = min(1.0, (adx_val - ADX_TREND_THRESHOLD) / 20.0) if adx_val > ADX_TREND_THRESHOLD else 0.0
        trend_match_factor = 0.5 if side == long_term_trend and long_term_trend != "Neutral" else 0.0
        
        # RRRを 1.5 から 3.0 の間で動的に計算
        rr_max_increase = SHORT_TERM_MAX_RRR - SHORT_TERM_MIN_RRR
        rr_base = SHORT_TERM_MIN_RRR + rr_max_increase * max(adx_factor, trend_match_factor) 
        
        # 5-B. エントリー価格の決定 (Market vs Limit)
        is_high_conviction = score >= 0.80 # 0.80以上を確信度が高いと見なす
        is_strong_trend = adx_val >= 35 # 35以上を強いトレンドと見なす
        
        use_market_entry = is_high_conviction and is_strong_trend # 両方が揃った場合のみMarket
        entry_type = "Market" if use_market_entry else "Limit"
        
        bb_mid = df['BBM_20_2.0'].iloc[-1] if 'BBM_20_2.0' in df.columns else price
        dc_mid = (df['DCU_20'].iloc[-1] + df['DCL_20'].iloc[-1]) / 2 if dc_cols_present else price
        
        entry = price 
        tp1 = 0
        sl = 0
        sl_dist = atr_val * SHORT_TERM_SL_MULTIPLIER 
        structural_sl_used = False

        if side == "ロング":
            if use_market_entry: entry = price
            else: entry = min(min(bb_mid, dc_mid), price) 
            
            atr_sl = entry - sl_dist
            if structural_sl_pivot > 0 and structural_sl_pivot < atr_sl and structural_sl_pivot < entry:
                 sl = structural_sl_pivot
                 structural_sl_used = True
            else:
                 sl = atr_sl
            
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry + tp_dist
            
        elif side == "ショート":
            if use_market_entry: entry = price
            else: entry = max(max(bb_mid, dc_mid), price) 
            
            atr_sl = entry + sl_dist
            if structural_sl_pivot > 0 and structural_sl_pivot > atr_sl and structural_sl_pivot > entry:
                 sl = structural_sl_pivot
                 structural_sl_used = True
            else:
                 sl = atr_sl
                 
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry - tp_dist
            
        else:
            entry_type = "N/A"
            entry, sl, tp1, rr_base = price, 0, 0, 0
        
        # 6. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD: # 0.75未満はNeutral
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
            "cci": cci_val, 
            "vwap_consistent": vwap_consistent,
            "ppo_hist": ppo_hist_val, 
            "dc_high": dc_high_val,
            "dc_low": dc_low_val,
            "stoch_k": stoch_k_val,
            "stoch_d": df[STOCHRSI_D].iloc[-1] if STOCHRSI_D in df.columns else 50.0,
            "stoch_filter_penalty": tech_data_defaults["stoch_filter_penalty"], 
            "volume_confirmation_bonus": volume_confirmation_bonus,
            "current_volume": current_volume,
            "average_volume": average_volume,
            "sentiment_fgi_proxy_bonus": sentiment_bonus,
            "structural_pivot_bonus": structural_pivot_bonus,
            "volume_ratio": volume_ratio,
            "structural_sl_used": structural_sl_used, 
            "long_term_reversal_penalty_value": penalty_value_lt,
            "macd_cross_penalty_value": penalty_value_macd
        }
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tech_data = tech_data_defaults 
        entry_type = "N/A"
        
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
        "entry_type": entry_type
    }
    
    return signal_candidate

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    
    # 0. 4hトレンドの事前計算
    long_term_trend = 'Neutral'
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce').astype('float64')
    df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms', utc=True)
    df_4h.set_index('timestamp', inplace=True)
    
    if status_4h == "Success" and len(df_4h.dropna(subset=['close'])) >= LONG_TERM_SMA_LENGTH:
        try:
            df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
            df_4h.dropna(subset=['sma'], inplace=True)
            if not df_4h.empty and 'sma' in df_4h.columns and not pd.isna(df_4h['sma'].iloc[-1]):
                last_price = df_4h['close'].iloc[-1]
                last_sma = df_4h['sma'].iloc[-1]
                if last_price > last_sma:
                    long_term_trend = 'Long'
                elif last_price < last_sma:
                    long_term_trend = 'Short'
        except Exception:
            pass 
            
    # 1. 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend, False) 
    ]
    
    results = await asyncio.gather(*tasks)
    
    # MTF スコアリングブーストロジック (v12.1.36から維持)
    signal_1h_item = next((r for r in results if r and r.get('timeframe') == '1h'), None)
    signal_15m_item = next((r for r in results if r and r.get('timeframe') == '15m'), None)

    if signal_1h_item and signal_15m_item:
        is_1h_strong_signal = signal_1h_item['score'] >= 0.80 # 1hの閾値を更に上げる
        is_direction_matched = signal_1h_item['side'] == signal_15m_item['side']
        if is_direction_matched and is_1h_strong_signal:
            signal_15m_item['score'] = min(1.0, signal_15m_item['score'] + 0.05)
            
    for result in results:
        if result and result.get('timeframe') == '4h':
            result.setdefault('tech_data', {})['long_term_trend'] = long_term_trend
    
    return [r for r in results if r is not None]


# ====================================================================================
# TASK SCHEDULER & MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CCXT_CLIENT_NAME, GLOBAL_MACRO_CONTEXT

    await initialize_ccxt_client()

    while True:
        try:
            current_time = time.time()
            
            await update_symbols_by_volume()
            monitor_symbols = CURRENT_MONITOR_SYMBOLS
            
            # 💡 グローバルマクロコンテキストを更新 (FGI Proxy, BTC/ETHトレンドなど)
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            results_list_of_lists = []
            
            for symbol in monitor_symbols:
                # 💡 更新されたマクロコンテキストを渡す
                result = await generate_integrated_signal(symbol, GLOBAL_MACRO_CONTEXT, CCXT_CLIENT_NAME)
                results_list_of_lists.append(result)
                
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            best_signals_per_symbol = {}
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                
                if signal.get('side') == 'Neutral' or signal.get('side') in ["DataShortage", "ExchangeError"]:
                    continue

                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals,
                        'rr_ratio': signal.get('rr_ratio', 0.0), 
                        'adx_val': signal.get('tech_data', {}).get('adx', 0.0), 
                        'atr_val': signal.get('tech_data', {}).get('atr_value', 1.0),
                        'symbol': symbol 
                    }
            
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: (
                    x['score'],     
                    x['rr_ratio'],  
                    x['adx_val'],   
                    -x['atr_val'],  
                    x['symbol']     
                ), 
                reverse=True
            )
            
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            notify_tasks = [] 
            
            if top_signals_to_notify:
                logging.info(f"🔔 高スコアシグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        msg = format_integrated_analysis_message(symbol, item['all_signals'], i + 1)
                        
                        if msg:
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {i+1}位, スコア: {item['score'] * 100:.2f}点)")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            
                            task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                            notify_tasks.append(task)
                            
                    else:
                        log_symbol = symbol.replace('-', '/')
                        logging.info(f"🕒 {log_symbol} はクールダウン期間中です。通知をスキップします。")
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            if notify_tasks:
                 await asyncio.gather(*notify_tasks, return_exceptions=True)

            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            error_message = str(e)
            error_name = "Unknown Error"
            
            if 'name' in error_message and 'is not defined' in error_message:
                try:
                    error_name = error_message.split(' ')[1].strip("'") 
                except IndexError:
                    pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v14.0.0 - Strict High-Conviction Logic")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v14.0.0 Startup initializing...") 
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
        "bot_version": "v14.0.0 - Strict High-Conviction Logic",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v14.0.0, Strict High-Conviction Logic)."}, status_code=200)

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
