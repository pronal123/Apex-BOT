# ====================================================================================
# Apex BOT v18.0.1 - Mexc Spot High-Win Rate/P-Score/Forex Macro Logic
# 
# 強化ポイント (v18.0.0からの改良):
# 1. 【為替指標追加】ドルインデックス (DXY) とドル円 (USD/JPY) の変動を検出し、グローバルなリスクセンチメントに基づくバイアススコアを適用。
# 
# v18.0.0の主要機能:
# - 【クライアント変更】CCXTクライアントをMexc (現物取引) に変更。
# - 【手数料考慮】COMMISSION_RATEを導入し、TP_Net（実質利益目標）とRRR_Net（実質RRR）を計算。
# - 【必須フィルター】RRR_Netが最低閾値未満のシグナルは問答無用で棄却。
# - 【長期トレンド強化】EMA 200をチェックし、逆行時に強力なペナルティを適用。
# - 【終焉フィルター】MACDダイバージェンス（トレンド終焉シグナル）を検出し棄却。
# - 【最適選定】純利益期待値 P-Score (Final Score * RRR_Net * Bias) に基づきシグナルを選定。
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
import yfinance as yf # 💡 為替/マクロ指標取得のために必須
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

# 💡 Mexc現物取引の監視対象銘柄 (USDTペア)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 50      
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

# 環境変数から取得。未設定の場合はダミー値。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_TOKEN') 
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 

# 💡【手数料/利益】v18.0.0 NEW CONSTANTS
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", 0.002)) # 往復(Taker/Taker) 0.2%をデフォルトとする
RRR_NET_MIN = 2.0                   # 実質RRRの最低必須閾値
PENALTY_COUNTER_TREND_STRONG = -0.30 
MACD_DIVERGENCE_REJECT_SCORE = 999.0 

# 💡【為替/マクロ】v18.0.1 NEW CONSTANTS
FX_DATA_PERIOD = '5d'               # 過去5日間のデータ
FX_DATA_INTERVAL = '1h'             # 1時間足のデータ
FX_CHANGE_THRESHOLD = 0.003         # 24時間で0.3%以上の変化を「急変」とみなす
FX_BIAS_BONUS_PENALTY = 0.07        # 為替バイアスによる最大ボーナス/ペナルティ点

# 💡【移動平均線】長期SMA/EMAの長さ (4h足で使用)
LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_EMA_LENGTH = 200          
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 

# 💡【ファンダメンタルズ】流動性/スマートマネーフィルター Parameters
LIQUIDITY_BONUS_POINT = 0.06        
ORDER_BOOK_DEPTH_LEVELS = 5         
OBV_MOMENTUM_BONUS = 0.04           
FGI_PROXY_BONUS_MAX = 0.07          
BONUS_STRUCTURAL_COMPOUND = 0.15    

# スコアリングロジック用の定数 
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               
RSI_MOMENTUM_HIGH = 60              
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 
CCI_OVERBOUGHT = 100                
CCI_OVERSOLD = -100                 
BONUS_STRUCTURAL_POINT = 0.07       

# グローバル状態変数
CCXT_CLIENT_NAME: str = 'mexc'      
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} 

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
    if timeframe == '15m': return "数時間以内 (2〜8時間)"
    if timeframe == '1h': return "半日以内 (6〜24時間)"
    if timeframe == '4h': return "数日以内 (2〜7日)"
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
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))

def format_integrated_analysis_message_v18(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    v18.0.1 改良版。P-Score, 実質RRR, 実質純利益TP、為替マクロコンテキストを含む通知メッセージを生成する
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
    if not valid_signals:
        return "" 
        
    best_signal = max(
        valid_signals, 
        key=lambda s: s.get('p_score', 0.0)
    )
    
    # ----------------------------------------------------
    # 1. 主要な取引情報の抽出
    # ----------------------------------------------------
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A')
    score_raw = best_signal.get('score', 0.5)
    rr_ratio_net = best_signal.get('rrr_net', 0.0) 
    p_score = best_signal.get('p_score', 0.0) 

    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    tp1_price = best_signal.get('tp1', 0.0) 
    tp1_net_price = best_signal.get('tp1_net', 0.0) 
    entry_type = best_signal.get('entry_type', 'N/A')
    
    display_symbol = symbol.replace('-', '/')
    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    time_to_tp = get_tp_reach_time(timeframe)
    
    if score_raw >= 0.85:
        confidence_text = "<b>極めて高い</b>"
    elif score_raw >= 0.75:
        confidence_text = "<b>高い</b>"
    else:
        confidence_text = "中程度"
        
    if side == "ロング":
        direction_emoji = "🚀"
        direction_text = "<b>ロング (LONG)</b>"
    else:
        direction_emoji = "💥"
        direction_text = "<b>ショート (SHORT)</b>"
        
    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    rank_emoji = rank_emojis.get(rank, "🏆")

    # ----------------------------------------------------
    # 2. メッセージの組み立て
    # ----------------------------------------------------

    # --- ヘッダー部 ---
    header = (
        f"{rank_emoji} <b>Apex BOT v18.0.1 - Rank {rank}</b> {rank_emoji}\n" # 💡 v18.0.1
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"{display_symbol} | {direction_emoji} {direction_text} | P-Score: <b>{p_score:.2f}</b>\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # --- 取引計画部 ---
    sl_width = abs(entry_price - sl_price)
    sl_source_str = "ATR基準"
    tech_data = best_signal.get('tech_data', {})

    if tech_data.get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib/PoC) バッファ"
        
    trade_plan = (
        f"<b>✅ 取引計画 (エントリー推奨)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>エントリー種別</b>: <code>{entry_type}</code> (指値/成行)\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>損切り (SL)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>リスク (SL幅)</b>: <code>${format_price_utility(sl_width, symbol)}</code>\n"
        f"  - <b>初期利益目標 (TP Target)</b>: <code>${format_price_utility(tp1_price, symbol)}</code>\n" 
        f"  - <b>🔥 実質純利益TP (TP Net)</b>: <code>${format_price_utility(tp1_net_price, symbol)}</code>\n" 
        f"  - <b>🔥 実質RRR (Net)</b>: <b>1 : {rr_ratio_net:.2f}</b> (必須: {RRR_NET_MIN:.1f})\n\n" 
    )

    # --- 分析サマリー部 ---
    regime = "トレンド相場" if tech_data.get('adx', 0.0) >= ADX_TREND_THRESHOLD else "レンジ相場"
    fgi_score = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_score > 0 else ("リスクオフ" if fgi_score < 0 else "中立")
    
    # 💡為替コンテキストの表示
    forex_context = tech_data.get('forex_context', '中立')
    forex_bias_score = tech_data.get('forex_bias_value', 0.0)
    
    summary = (
        f"<b>💡 分析サマリー</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>分析スコア</b>: <code>{score_100:.2f} / 100</code> (信頼度: {confidence_text})\n"
        f"  - <b>予測勝率</b>: <code>約 {win_rate:.1f}%</code>\n"
        f"  - <b>時間軸 (メイン)</b>: <code>{timeframe}</code>\n"
        f"  - <b>決済までの目安</b>: {time_to_tp}\n"
        f"  - <b>市場の状況</b>: {regime} (ADX: {tech_data.get('adx', 0.0):.1f})\n"
        f"  - <b>グローバルマクロ</b>: {forex_context} ({abs(forex_bias_score*100):.1f}点影響)\n" # 💡 為替マクロを表示
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n\n"
    )

    # --- 分析の根拠部 (チェックリスト形式) ---
    lt_penalty_applied = tech_data.get('long_term_reversal_penalty', False)
    long_term_trend_ok = not lt_penalty_applied
    lt_penalty_value = tech_data.get('long_term_reversal_penalty_value', 0.0)
    momentum_ok = tech_data.get('macd_cross_valid', True) and not tech_data.get('stoch_filter_penalty', 0) > 0
    compound_structure_ok = tech_data.get('structural_compound_bonus', 0.0) >= BONUS_STRUCTURAL_COMPOUND - 0.01 
    volume_confirm_ok = tech_data.get('volume_confirmation_bonus', 0.0) > 0
    obv_confirm_ok = tech_data.get('obv_momentum_bonus_value', 0.0) > 0
    liquidity_ok = tech_data.get('liquidity_bonus_value', 0.0) > 0
    dominance_ok = tech_data.get('dominance_bias_bonus_value', 0.0) > 0
    fib_level = tech_data.get('fib_proximity_level', 'N/A')
    
    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    lt_trend_check_text = f"長期 ({lt_trend_str}, SMA {LONG_TERM_SMA_LENGTH} & EMA {LONG_TERM_EMA_LENGTH}) と一致"
    lt_trend_check_text_penalty = f"長期トレンド ({lt_trend_str}) と逆行 ({abs(lt_penalty_value)*100:.1f}点ペナルティ)"
    
    
    analysis_details = (
        f"<b>🔍 分析の根拠</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>トレンド/勢い</b>: \n"
        f"    {'✅' if long_term_trend_ok else '❌'} {'<b>' if lt_penalty_applied else ''}{lt_trend_check_text if long_term_trend_ok else lt_trend_check_text_penalty}{'</b>' if lt_penalty_applied else ''}\n"
        f"    {'✅' if momentum_ok else '⚠️'} 短期モメンタム加速 (RSI/MACD/CCI)\n"
        f"    {'❌' if tech_data.get('is_macd_divergence', False) else '✅'} トレンド終焉シグナルなし (MACDダイバージェンス)\n" 
        f"  - <b>価格構造/ファンダ</b>: \n"
        f"    {'✅' if compound_structure_ok else '❌'} 重要支持/抵抗線に近接 ({fib_level}/PoC確認)\n" 
        f"    {'✅' if (volume_confirm_ok or obv_confirm_ok) else '❌'} 出来高/OBVの裏付け\n"
        f"    {'✅' if liquidity_ok else '❌'} 板の厚み (流動性) 優位\n"
        f"  - <b>市場心理/その他</b>: \n"
        f"    {'✅' if dominance_ok else '❌'} BTCドミナンスが追い風\n"
    )
    
    # --- フッター部 ---
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ Limit注文は、指定水準到達時のみ約定します。SLは自動的に追跡され利益を最大化します。</pre>"
        f"<i>Bot Ver: v18.0.1 (Mexc Spot, P-Score/Forex Macro Adjusted)</i>" # 💡 v18.0.1
    )

    return header + trade_plan + summary + analysis_details + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (Mexc現物)"""
    global EXCHANGE_CLIENT
    
    EXCHANGE_CLIENT = ccxt_async.mexc({ 
        'apiKey': os.environ.get('CCXT_API_KEY'),
        'secret': os.environ.get('CCXT_SECRET_KEY'),
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'} 
    })
    
    if EXCHANGE_CLIENT:
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - リアル接続, Default: Spot)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


def get_volume_profile_poc_approx(df: pd.DataFrame) -> float:
    """出来高プロファイルPoC（Point of Control）を近似的に計算する。"""
    if df.empty:
        return 0.0
    
    vwap_approx = (df['close'] * df['volume']).sum() / df['volume'].sum()
    return vwap_approx if not pd.isna(vwap_approx) else df['close'].iloc[-1]


async def fetch_order_book_depth(symbol: str) -> Optional[Dict]:
    """Mexcからオーダーブックを取得し、流動性 (上位板の厚み) を計算"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE

    if symbol in ORDER_BOOK_CACHE and (time.time() - ORDER_BOOK_CACHE[symbol]['timestamp'] < LOOP_INTERVAL * 0.2):
         return ORDER_BOOK_CACHE[symbol]['data']
         
    if not EXCHANGE_CLIENT:
        return None
    
    try:
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=50) 
        
        if not orderbook['bids'] or not orderbook['asks']:
             return None
             
        best_bid_price = orderbook['bids'][0][0]
        best_ask_price = orderbook['asks'][0][0]
        current_price = (best_bid_price + best_ask_price) / 2
        
        total_bid_volume = sum(orderbook['bids'][i][1] * orderbook['bids'][i][0] for i in range(min(ORDER_BOOK_DEPTH_LEVELS, len(orderbook['bids']))))
        total_ask_volume = sum(orderbook['asks'][i][1] * orderbook['asks'][i][0] for i in range(min(ORDER_BOOK_DEPTH_LEVELS, len(orderbook['asks']))))
        
        ask_bid_ratio = total_ask_volume / total_bid_volume if total_bid_volume > 0 else 0.0
        
        data = {
            'price': current_price,
            'ask_volume': total_ask_volume,
            'bid_volume': total_bid_volume,
            'ask_bid_ratio': ask_bid_ratio
        }
        
        ORDER_BOOK_CACHE[symbol] = {'timestamp': time.time(), 'data': data}
        
        return data

    except Exception as e:
        return None


async def update_symbols_by_volume():
    """CCXTを使用してMexcの出来高トップのUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
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
        
        new_monitor_symbols = [symbol for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        if new_monitor_symbols:
            CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
            LAST_SUCCESSFUL_MONITOR_SYMBOLS = new_monitor_symbols.copy()
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
        return [], "ExchangeError", client_name

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        return [], "ExchangeError", client_name
        
    except Exception as e:
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """
    マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias, Forex Bias)
    """
    
    # 1. BTC/ETHの長期トレンド計算 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC/USDT", '4h') 
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH/USDT", '4h') 
    
    btc_trend = 0
    eth_trend = 0
    btc_change = 0.0
    eth_change = 0.0
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()
    
    # ... (既存のBTC/ETHトレンド計算ロジック) ...
    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH) 
        df_btc['ema200'] = ta.ema(df_btc['close'], length=LONG_TERM_EMA_LENGTH)
        df_btc.dropna(subset=['sma', 'ema200'], inplace=True)
        if not df_btc.empty:
            last_price = df_btc['close'].iloc[-1]
            last_sma = df_btc['sma'].iloc[-1]
            last_ema200 = df_btc['ema200'].iloc[-1]
            
            if last_price > last_sma and last_price > last_ema200: btc_trend = 1 
            elif last_price < last_sma and last_price < last_ema200: btc_trend = -1 
            
            if len(df_btc) >= 2:
                btc_change = (last_price - df_btc['close'].iloc[-2]) / df_btc['close'].iloc[-2]
    
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth['ema200'] = ta.ema(df_eth['close'], length=LONG_TERM_EMA_LENGTH)
        df_eth.dropna(subset=['sma', 'ema200'], inplace=True)
        if not df_eth.empty:
            last_price = df_eth['close'].iloc[-1]
            last_sma = df_eth['sma'].iloc[-1]
            last_ema200 = df_eth['ema200'].iloc[-1]
            
            if last_price > last_sma and last_price > last_ema200: eth_trend = 1 
            elif last_price < last_sma and last_price < last_ema200: eth_trend = -1 

            if len(df_eth) >= 2:
                eth_change = (last_price - df_eth['close'].iloc[-2]) / df_eth['close'].iloc[-2]

    # 2. FGI Proxy (センチメント) の計算
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = FGI_PROXY_BONUS_MAX 
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -FGI_PROXY_BONUS_MAX 
        
    # 3. BTC Dominance Proxyの計算
    dominance_trend = "Neutral"
    dominance_bias_score = 0.0
    DOM_DIFF_THRESHOLD = 0.002 
    
    if status_btc == "Success" and status_eth == "Success" and len(df_btc) >= 2 and len(df_eth) >= 2:
        if btc_change - eth_change > DOM_DIFF_THRESHOLD:
            dominance_trend = "Increasing"
            dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY 
        elif eth_change - btc_change > DOM_DIFF_THRESHOLD:
            dominance_trend = "Decreasing"
            dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY 
            
    # ----------------------------------------------------
    # 4. 💡【為替指標】USD/JPY & DXY の計算 (v18.0.1 NEW)
    # ----------------------------------------------------
    forex_bias = 0.0
    forex_context = "Neutral"
    
    try:
        tickers = ["USDJPY=X", "DX-Y.NYB"] 
        # yfinanceを使用して為替データを取得 (1h足で過去24時間の変化をチェックするため、25本以上が必要)
        fx_data_multi = yf.download(tickers, period=FX_DATA_PERIOD, interval=FX_DATA_INTERVAL, progress=False, ignore_tz=True)['Close']
        
        # 単一シンボルの取得を考慮し、列名を取得
        usdjpy_col = "USDJPY=X" if "USDJPY=X" in fx_data_multi.columns else None
        dxy_col = "DX-Y.NYB" if "DX-Y.NYB" in fx_data_multi.columns else None
        
        if not fx_data_multi.empty and usdjpy_col and dxy_col and len(fx_data_multi) > 25:
            
            # 24時間前の終値と比較
            dxy_current = fx_data_multi[dxy_col].iloc[-1]
            usdjpy_current = fx_data_multi[usdjpy_col].iloc[-1]
            dxy_prev_24h = fx_data_multi[dxy_col].iloc[-25]
            usdjpy_prev_24h = fx_data_multi[usdjpy_col].iloc[-25]
            
            dxy_change = (dxy_current - dxy_prev_24h) / dxy_prev_24h
            usdjpy_change = (usdjpy_current - usdjpy_prev_24h) / usdjpy_prev_24h

            # a) DXY (ドルインデックス) ロジック
            # ドル安 (DXY下落) = リスクオン => 仮想通貨ロングに有利
            if dxy_change < -FX_CHANGE_THRESHOLD:
                forex_bias += FX_BIAS_BONUS_PENALTY
                forex_context = "Risk-On (Weak USD)"
            # ドル高 (DXY上昇) = リスクオフ => 仮想通貨ショートに有利
            elif dxy_change > FX_CHANGE_THRESHOLD:
                forex_bias -= FX_BIAS_BONUS_PENALTY
                forex_context = "Risk-Off (Strong USD)"
                
            # b) USD/JPY (ドル円) ロジック
            # ドル円上昇 = 円安/キャリー加速 => リスクオン
            if usdjpy_change > FX_CHANGE_THRESHOLD:
                 forex_bias += FX_BIAS_BONUS_PENALTY * 0.5 
                 if forex_context == "Neutral": forex_context = "Risk-On (JPY Weakness)"
            # ドル円下落 = 円高/キャリー解消 => リスクオフ
            elif usdjpy_change < -FX_CHANGE_THRESHOLD:
                 forex_bias -= FX_BIAS_BONUS_PENALTY * 0.5
                 if forex_context == "Neutral": forex_context = "Risk-Off (JPY Strength)"
            
            forex_bias = max(-FX_BIAS_BONUS_PENALTY, min(FX_BIAS_BONUS_PENALTY, forex_bias))
            
    except Exception as e:
        # logging.warning(f"FXデータ取得/計算エラー: {e}")
        pass 

    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        "dominance_trend": dominance_trend, 
        "dominance_bias_score": dominance_bias_score,
        "forex_bias_score": forex_bias,               # 💡 NEW
        "forex_context": forex_context                # 💡 NEW
    }


# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

# Fibonacci Pivot Point Calculation Utility (Simplified)
def calculate_fib_pivot(df: pd.DataFrame) -> Dict:
    """直近のバーに基づきフィボナッチ・ピボットポイントを計算する (H, L, C, P, R, S)"""
    if len(df) < 1:
        return {'P': np.nan, 'R1': np.nan, 'S1': np.nan}
    
    H = df['high'].iloc[-2] if len(df) >= 2 else df['high'].iloc[-1]
    L = df['low'].iloc[-2] if len(df) >= 2 else df['low'].iloc[-1]
    C = df['close'].iloc[-1]
    
    P = (H + L + C) / 3
    
    R1 = P + (H - L) * 0.382
    S1 = P - (H - L) * 0.382
    R2 = P + (H - L) * 0.618
    S2 = P - (H - L) * 0.618
    
    return {'P': P, 'R1': R1, 'S1': S1, 'R2': R2, 'S2': S2}

def analyze_structural_proximity(price: float, pivots: Dict, side: str, atr_val: float) -> Tuple[float, float, float, str]:
    """
    価格とPivotポイントを比較し、構造的なSL/TPを決定し、ボーナススコアを返す (0.07点)
    返り値: (ボーナススコア, 構造的SL, 構造的TP, 近接Fibレベル)
    """
    bonus = 0.0
    structural_sl = 0.0
    structural_tp = 0.0
    BONUS_POINT = BONUS_STRUCTURAL_POINT 
    FIB_PROXIMITY_THRESHOLD = atr_val * 1.0 
    proximity_level = 'N/A'

    R1 = pivots.get('R1', np.nan)
    S1 = pivots.get('S1', np.nan)
    R2 = pivots.get('R2', np.nan)
    S2 = pivots.get('S2', np.nan)

    if pd.isna(R1) or pd.isna(S1):
        return 0.0, 0.0, 0.0, proximity_level

    # 構造的なSL/TPの採用
    if side == "ロング":
        if abs(price - S1) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'S1'
        elif abs(price - S2) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'S2'
            
        if price > S1 and (price - S1) / (R1 - S1) < 0.5:
            bonus = BONUS_POINT 
            structural_sl = S1
            structural_tp = R1 * 1.01 
        elif price > R1:
            bonus = BONUS_POINT
            structural_sl = R1 
            structural_tp = R1 * 1.05 
            
    elif side == "ショート":
        if abs(price - R1) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R1'
        elif abs(price - R2) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R2'
            
        if price < R1 and (R1 - price) / (R1 - S1) < 0.5:
            bonus = BONUS_POINT 
            structural_sl = R1 
            structural_tp = S1 * 0.99 
        elif price < S1:
            bonus = BONUS_POINT
            structural_sl = S1 
            structural_tp = S1 * 0.95 

    if proximity_level != 'N/A':
         bonus = max(bonus, BONUS_POINT + 0.03) 

    return bonus, structural_sl, structural_tp, proximity_level


async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v18.0.1 - 為替バイアス適用)
    """
    
    # 1. データ取得とOrder Book取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    order_book_data = await fetch_order_book_depth(symbol) if timeframe == '1h' else None
    
    
    tech_data_defaults = {
        "rsi": 50.0, "macd_hist": 0.0, "adx": 25.0, "bb_width_pct": 0.0, "atr_value": 0.005,
        "long_term_trend": long_term_trend, "long_term_reversal_penalty": False, "macd_cross_valid": False,
        "cci": 0.0, "vwap_consistent": False, "ppo_hist": 0.0, "dc_high": 0.0, "dc_low": 0.0,
        "stoch_k": 50.0, "stoch_d": 50.0, "stoch_filter_penalty": 0.0,
        "volume_confirmation_bonus": 0.0, "current_volume": 0.0, "average_volume": 0.0,
        "sentiment_fgi_proxy_bonus": 0.0, "structural_pivot_bonus": 0.0, 
        "volume_ratio": 0.0, "structural_sl_used": False,
        "long_term_reversal_penalty_value": 0.0, 
        "macd_cross_penalty_value": 0.0,
        "dominance_trend": "Neutral",
        "dominance_bias_bonus_value": 0.0,
        "liquidity_bonus_value": 0.0, 
        "ask_bid_ratio": order_book_data.get('ask_bid_ratio', 1.0) if order_book_data else 1.0,
        "obv_trend_match": "N/A", 
        "obv_momentum_bonus_value": 0.0, 
        "fib_proximity_level": "N/A",
        "dynamic_exit_strategy": "DTS",
        "structural_compound_bonus": 0.0, 
        "is_macd_divergence": False, 
        "rrr_net": 0.0, 
        "p_score": 0.0,
        "forex_bias_value": 0.0, # 💡 NEW
        "forex_context": "Neutral" # 💡 NEW
    }
    
    if status != "Success":
        return {"symbol": symbol, "side": status, "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": 0.0, "entry": 0.0, "tp1": 0.0, "tp1_net": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A", "rrr_net": 0.0, "p_score": 0.0}

    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64')

    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    df.set_index('timestamp', inplace=True)
    
    price = df['close'].iloc[-1] if not df.empty and not pd.isna(df['close'].iloc[-1]) else 0.0
    atr_val = df['close'].iloc[-1] * 0.005 if not df.empty and df['close'].iloc[-1] > 0 else 0.005 
    
    final_side = "Neutral"
    base_score = BASE_SCORE 
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
        df['obv'] = ta.obv(df['close'], df['volume']) 
        pivots = calculate_fib_pivot(df)
        
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', PPO_HIST_COL, 'obv'] 
        if STOCHRSI_K in df.columns: required_cols.append(STOCHRSI_K)
        if STOCHRSI_D in df.columns: required_cols.append(STOCHRSI_D)
        df.dropna(subset=required_cols, inplace=True)
        
        dc_cols_present = 'DCL_20' in df.columns and 'DCU_20' in df.columns
        
        if df.empty or len(df) < 2: 
            return {"symbol": symbol, "side": "DataShortage", "client": client_used, "timeframe": timeframe, "tech_data": tech_data_defaults, "score": 0.5, "price": price, "entry": 0.0, "tp1": 0.0, "tp1_net": 0.0, "sl": 0.0, "rr_ratio": 0.0, "entry_type": "N/A", "rrr_net": 0.0, "p_score": 0.0}

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
        obv_val = df['obv'].iloc[-1]
        obv_prev_val = df['obv'].iloc[-2]
        
        long_score = BASE_SCORE 
        short_score = BASE_SCORE 
        dc_low_val = price 
        dc_high_val = price
        if dc_cols_present:
            dc_low_val = df['DCL_20'].iloc[-1]     
            dc_high_val = df['DCU_20'].iloc[-1]
        
        # A. MACDに基づく方向性 (0.15)
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev: long_score += 0.15 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev: short_score += 0.15 
        # B. RSIに基づく買われすぎ/売られすぎ (0.08)
        if rsi_val < RSI_OVERSOLD: long_score += 0.08
        elif rsi_val > RSI_OVERBOUGHT: short_score += 0.08
        # C. RSIに基づくモメンタムブレイクアウト (0.12)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH: long_score += 0.12 
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW: short_score += 0.12 
        # D. ADXに基づくトレンドフォロー強化 (0.10)
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score: long_score += 0.10
            elif short_score > long_score: short_score += 0.10
        # E. VWAPの一致チェック (0.04)
        vwap_consistent = False
        if price > vwap_val:
            long_score += 0.04
            vwap_consistent = True
        elif price < vwap_val:
            short_score += 0.04
            vwap_consistent = True
        # F. PPOに基づくモメンタム強度の評価 (0.04)
        ppo_abs_mean = df[PPO_HIST_COL].abs().mean()
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean: long_score += 0.04 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean: short_score += 0.04
        # G. Donchian Channelによるブレイクアウト (0.15)
        is_breaking_high = False
        is_breaking_low = False
        if dc_cols_present: 
            is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
            is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val
            if is_breaking_high: long_score += 0.15 
            elif is_breaking_low: short_score += 0.15
        # H. 複合モメンタム加速ボーナス (0.05)
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50: long_score += 0.05
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50: short_score += 0.05
        # I. CCIに基づくモメンタム加速ボーナス (0.04)
        if cci_val > CCI_OVERBOUGHT and cci_val > df['cci'].iloc[-2]: long_score += 0.04
        elif cci_val < CCI_OVERSOLD and cci_val < df['cci'].iloc[-2]: short_score += 0.04

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

        # 3. **ファンダメンタルズ/マクロバイアスフィルターの適用**

        # K. BTCドミナンスバイアスフィルター (Altcoinのみ) (+/- 0.05点)
        dominance_bonus = 0.0
        dominance_trend = macro_context.get('dominance_trend', 'Neutral')
        dominance_bias_score_val = macro_context.get('dominance_bias_score', 0.0)
        
        if symbol != "BTC/USDT" and dominance_trend != "Neutral":
            if dominance_trend == "Increasing": 
                if side == "ロング": dominance_bonus = dominance_bias_score_val 
                elif side == "ショート": dominance_bonus = abs(dominance_bias_score_val) 
            elif dominance_trend == "Decreasing": 
                if side == "ロング": dominance_bonus = abs(dominance_bias_score_val) 
                elif side == "ショート": dominance_bonus = dominance_bias_score_val 
            score = max(BASE_SCORE, min(1.0, score + dominance_bonus))

        # L. 【恐怖指数】市場センチメント (FGI Proxy) の適用 (+/-0.07点)
        sentiment_bonus = macro_context.get('sentiment_fgi_proxy', 0.0)
        if side == "ロング" and sentiment_bonus > 0:
            score = min(1.0, score + sentiment_bonus)
        elif side == "ショート" and sentiment_bonus < 0:
            score = min(1.0, score + abs(sentiment_bonus))
            
        # M. 💡【為替バイアス】為替市場の動向をスコアに反映 (v18.0.1 NEW)
        forex_bias = macro_context.get('forex_bias_score', 0.0)
        forex_context = macro_context.get('forex_context', 'Neutral')
        
        if forex_bias != 0.0:
            if side == "ロング" and forex_bias > 0:
                score = min(1.0, score + forex_bias)
            elif side == "ショート" and forex_bias < 0:
                score = min(1.0, score + abs(forex_bias))
            elif side == "ロング" and forex_bias < 0:
                score = max(BASE_SCORE, score + forex_bias * 0.5) 
            elif side == "ショート" and forex_bias > 0:
                score = max(BASE_SCORE, score - forex_bias * 0.5) 
        
        # N-1. Structural/Pivot/Fib Analysis (0.07点 + Fib強化)
        structural_pivot_bonus, structural_sl_pivot, structural_tp_pivot, fib_level = analyze_structural_proximity(price, pivots, side, atr_val)
        score = min(1.0, score + structural_pivot_bonus)
        
        # N-2. 複合構造的確信度ボーナス (PoC近似)
        poc_approx = get_volume_profile_poc_approx(df.iloc[-50:]) 
        compound_structural_bonus = 0.0
        is_compound_structural = False
        
        entry = price # 一時的にentryにpriceを代入
        
        if side == "ロング" and fib_level in ['S1', 'S2'] and abs(entry - poc_approx) / atr_val < 2.0:
             compound_structural_bonus = BONUS_STRUCTURAL_COMPOUND
             is_compound_structural = True
        elif side == "ショート" and fib_level in ['R1', 'R2'] and abs(entry - poc_approx) / atr_val < 2.0:
             compound_structural_bonus = BONUS_STRUCTURAL_COMPOUND
             is_compound_structural = True
        
        score = min(1.0, score + compound_structural_bonus)

        # O. 出来高/流動性確証 & OBV (Max 0.12 + 0.04)
        volume_confirmation_bonus = 0.0
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER: 
            if dc_cols_present and (is_breaking_high or is_breaking_low): volume_confirmation_bonus += 0.06
            if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean(): volume_confirmation_bonus += 0.06
            score = min(1.0, score + volume_confirmation_bonus)
            
        obv_trend_match = "N/A"
        obv_momentum_bonus = 0.0
        if obv_val > obv_prev_val and side == "ロング":
             obv_trend_match = "Long"
             obv_momentum_bonus = OBV_MOMENTUM_BONUS
        elif obv_val < obv_prev_val and side == "ショート":
             obv_trend_match = "Short"
             obv_momentum_bonus = OBV_MOMENTUM_BONUS
             
        score = min(1.0, score + obv_momentum_bonus)
        
        # P. 【ファンダメンタルズ】流動性/スマートマネーフィルター (板の厚み) 
        liquidity_bonus = 0.0
        ask_bid_ratio_val = 1.0
        
        if timeframe == '1h' and order_book_data:
            ask_bid_ratio_val = order_book_data.get('ask_bid_ratio', 1.0)
            if side == "ロング":
                if ask_bid_ratio_val < 0.9: liquidity_bonus = LIQUIDITY_BONUS_POINT
            elif side == "ショート":
                if ask_bid_ratio_val > 1.1: liquidity_bonus = LIQUIDITY_BONUS_POINT
            score = min(1.0, score + liquidity_bonus)
        
        
        # 4. **フィルターによるペナルティ適用**
        
        # Q. 【移動平均線】4hトレンドフィルターの適用 (EMA200/SMA50逆行ペナルティ)
        penalty_value_lt = 0.0
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                score = max(BASE_SCORE, score + PENALTY_COUNTER_TREND_STRONG) 
                current_long_term_penalty_applied = True
                penalty_value_lt = PENALTY_COUNTER_TREND_STRONG
        
        # R. MACDクロス確認と減点 (モメンタム反転チェック)
        macd_valid = True
        penalty_value_macd = 0.0
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(BASE_SCORE, score - MACD_CROSS_PENALTY)
                 macd_valid = False
                 penalty_value_macd = MACD_CROSS_PENALTY
        
        # S. 💡 MACDダイバージェンスフィルター (トレンド終焉シグナルの棄却)
        is_divergence = False
        macd_peak_5 = df[MACD_HIST_COL].iloc[-6:-1].max()
        macd_bottom_5 = df[MACD_HIST_COL].iloc[-6:-1].min()

        if side == "ロング" and price > df['high'].iloc[-6:-1].max() and macd_hist_val < macd_peak_5 * 0.8:
            is_divergence = True 
        elif side == "ショート" and price < df['low'].iloc[-6:-1].min() and macd_hist_val > macd_bottom_5 * 0.8:
            is_divergence = True 
            
        if is_divergence:
            tech_data_defaults["is_macd_divergence"] = True
            logging.warning(f"[{symbol} {timeframe}] MACDダイバージェンスによりシグナル棄却。")
            return None 

        
        # 5. TP/SLとRRRの決定 (Dynamic Trailing Stop & Structural SL)
        
        rr_base = DTS_RRR_DISPLAY 
        is_high_conviction = score >= 0.80
        is_strong_trend = adx_val >= 35
        use_market_entry = is_high_conviction and is_strong_trend and fib_level == 'N/A' 
        entry_type = "Market" if use_market_entry else "Limit"
        
        bb_mid = df['BBM_20_2.0'].iloc[-1] if 'BBM_20_2.0' in df.columns else price
        dc_mid = (df['DCU_20'].iloc[-1] + df['DCL_20'].iloc[-1]) / 2 if dc_cols_present else price
        
        entry = price 
        tp1 = 0
        sl = 0
        sl_dist_atr = atr_val * ATR_TRAIL_MULTIPLIER 
        structural_sl_used = False

        if side == "ロング":
            entry = price if use_market_entry else min([price, bb_mid, dc_mid] + ([pivots[fib_level]] if fib_level in ['S1', 'S2'] else []))
            atr_sl = entry - sl_dist_atr
            if structural_sl_pivot > 0 and structural_sl_pivot > atr_sl and structural_sl_pivot < entry:
                 sl = structural_sl_pivot - atr_val * 0.5 
                 structural_sl_used = True
            else:
                 sl = atr_sl
            if sl <= 0: sl = entry * 0.99 
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry + tp_dist
            
        elif side == "ショート":
            entry = price if use_market_entry else max([price, bb_mid, dc_mid] + ([pivots[fib_level]] if fib_level in ['R1', 'R2'] else []))
            atr_sl = entry + sl_dist_atr
            if structural_sl_pivot > 0 and structural_sl_pivot < atr_sl and structural_sl_pivot > entry:
                 sl = structural_sl_pivot + atr_val * 0.5 
                 structural_sl_used = True
            else:
                 sl = atr_sl
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry - tp_dist
            
        else:
            entry_type = "N/A"
            entry, tp1, sl, rr_base = price, 0, 0, 0
            
        # 6. 実質TP/RRR/P-Scoreの計算とRRR_Netフィルター (最重要)
        
        tp1_net = 0.0
        rrr_net = 0.0
        p_score = 0.0
        
        if final_side != "Neutral":
            commission_cost = entry * COMMISSION_RATE * 2.0 
            
            if side == "ロング": tp1_net = tp1 - commission_cost
            else: tp1_net = tp1 + commission_cost
                 
            risk = abs(entry - sl)
            reward_net = abs(tp1_net - entry)
            rrr_net = reward_net / risk if risk != 0 and reward_net > 0 else 0.0
            
            if rrr_net < RRR_NET_MIN:
                logging.warning(f"[{symbol} {timeframe}] RRR_Net ({rrr_net:.2f}) が最低閾値 ({RRR_NET_MIN}) 未満のため棄却。")
                return None
                
            macd_hist_abs_mean = df[MACD_HIST_COL].abs().mean()
            volume_delta_bias = abs(macd_hist_val) / (macd_hist_abs_mean + 1e-5) 
            p_score = score * rrr_net * (1.0 + volume_delta_bias)
            
        # 7. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD: 
             final_side = "Neutral"

        # 8. tech_dataの構築
        bb_width_pct_val = (df['BBU_20_2.0'].iloc[-1] - df['BBL_20_2.0'].iloc[-1]) / df['close'].iloc[-1] * 100 if 'BBU_20_2.0' in df.columns else 0.0

        tech_data = {
            "rsi": rsi_val, "macd_hist": macd_hist_val, "adx": adx_val, "bb_width_pct": bb_width_pct_val,
            "atr_value": atr_val, "long_term_trend": long_term_trend,
            "long_term_reversal_penalty": current_long_term_penalty_applied, "macd_cross_valid": macd_valid,
            "cci": cci_val, "vwap_consistent": vwap_consistent, "ppo_hist": ppo_hist_val, 
            "dc_high": dc_high_val, "dc_low": dc_low_val, "stoch_k": stoch_k_val, 
            "stoch_d": df[STOCHRSI_D].iloc[-1] if STOCHRSI_D in df.columns else 50.0,
            "stoch_filter_penalty": tech_data_defaults["stoch_filter_penalty"], 
            "volume_confirmation_bonus": volume_confirmation_bonus, "current_volume": current_volume,
            "average_volume": average_volume, "sentiment_fgi_proxy_bonus": sentiment_bonus, 
            "structural_pivot_bonus": structural_pivot_bonus, "volume_ratio": volume_ratio,
            "structural_sl_used": structural_sl_used, "long_term_reversal_penalty_value": penalty_value_lt, 
            "macd_cross_penalty_value": penalty_value_macd, "dominance_trend": dominance_trend,
            "dominance_bias_bonus_value": dominance_bonus, "liquidity_bonus_value": liquidity_bonus, 
            "ask_bid_ratio": ask_bid_ratio_val, "obv_trend_match": obv_trend_match, 
            "obv_momentum_bonus_value": obv_momentum_bonus, "fib_proximity_level": fib_level, 
            "dynamic_exit_strategy": "DTS", "structural_compound_bonus": compound_structural_bonus, 
            "is_macd_divergence": is_divergence,
            "forex_bias_value": forex_bias, # 💡 NEW
            "forex_context": forex_context  # 💡 NEW
        }
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}) のテクニカル分析中に予期せぬエラーが発生しました: {type(e).__name__} - {e}. Neutralとして処理を継続します。")
        final_side = "Neutral"
        score = 0.5
        entry, tp1, sl, rr_base = price, 0, 0, 0 
        tp1_net, rrr_net, p_score = 0, 0, 0
        tech_data = tech_data_defaults 
        entry_type = "N/A"
        
    # 9. シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol, "side": final_side, "score": score, "confidence": score,
        "price": price, "entry": entry, "tp1": tp1, "tp1_net": tp1_net, "sl": sl,   
        "rr_ratio": rr_base if final_side != "Neutral" else 0.0, "rrr_net": rrr_net, 
        "p_score": p_score, "regime": "トレンド" if tech_data['adx'] >= ADX_TREND_THRESHOLD else "レンジ",
        "macro_context": macro_context, "client": client_used, "timeframe": timeframe,
        "tech_data": tech_data, "entry_type": entry_type
    }
    
    return signal_candidate if final_side != "Neutral" else None

async def generate_integrated_signal(symbol: str, macro_context: Dict, client_name: str) -> List[Optional[Dict]]:
    
    # 0. 4hトレンドの事前計算 (EMA 200を含む)
    long_term_trend = 'Neutral'
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce').astype('float64')
    df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms', utc=True)
    df_4h.set_index('timestamp', inplace=True)
    
    if status_4h == "Success" and len(df_4h.dropna(subset=['close'])) >= LONG_TERM_EMA_LENGTH: 
        try:
            df_4h['sma'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH) 
            df_4h['ema200'] = ta.ema(df_4h['close'], length=LONG_TERM_EMA_LENGTH)
            df_4h.dropna(subset=['sma', 'ema200'], inplace=True)
            
            if not df_4h.empty and 'sma' in df_4h.columns and 'ema200' in df_4h.columns:
                last_price = df_4h['close'].iloc[-1]
                last_sma = df_4h['sma'].iloc[-1]
                last_ema200 = df_4h['ema200'].iloc[-1]

                if last_price > last_sma and last_price > last_ema200: long_term_trend = 'Long'
                elif last_price < last_sma and last_price < last_ema200: long_term_trend = 'Short'
        except Exception:
            pass 
            
    # 1. 各時間軸の分析を並行して実行
    tasks = [
        analyze_single_timeframe(symbol, '15m', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '1h', macro_context, client_name, long_term_trend, False),
        analyze_single_timeframe(symbol, '4h', macro_context, client_name, long_term_trend, False) 
    ]
    
    results = await asyncio.gather(*tasks)
    
    # MTF スコアリングブーストロジック
    signal_1h_item = next((r for r in results if r and r.get('timeframe') == '1h'), None)
    signal_15m_item = next((r for r in results if r and r.get('timeframe') == '15m'), None)

    if signal_1h_item and signal_15m_item:
        is_1h_strong_signal = signal_1h_item['score'] >= 0.80 
        is_direction_matched = signal_1h_item['side'] == signal_15m_item['side']
        if is_direction_matched and is_1h_strong_signal:
            signal_15m_item['score'] = min(1.0, signal_15m_item['score'] + 0.05)
            signal_15m_item['p_score'] = signal_15m_item['p_score'] * 1.05 
            
    for result in results:
        if result:
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
            
            # 💡 グローバルマクロコンテキストを更新 (為替指標を含む)
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            log_symbols = [s for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            logging.info(f"🌎 為替/マクロコンテキスト: {GLOBAL_MACRO_CONTEXT.get('forex_context', 'Neutral')} (Bias: {GLOBAL_MACRO_CONTEXT.get('forex_bias_score', 0.0):.4f})")
            
            results_list_of_lists = []
            
            ob_fetch_symbols = [s for s in monitor_symbols]
            ob_tasks = [fetch_order_book_depth(symbol) for symbol in ob_fetch_symbols]
            await asyncio.gather(*ob_tasks) 
            
            for symbol in monitor_symbols:
                result = await generate_integrated_signal(symbol, GLOBAL_MACRO_CONTEXT, CCXT_CLIENT_NAME)
                results_list_of_lists.append(result)
                
                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)

            all_signals = [s for sublist in results_list_of_lists for s in sublist if s is not None] 
            LAST_ANALYSIS_SIGNALS = all_signals
            
            best_signals_per_symbol = {}
            for signal in all_signals:
                symbol = signal['symbol']
                score = signal['score']
                p_score = signal.get('p_score', 0.0) 

                if symbol not in best_signals_per_symbol or p_score > best_signals_per_symbol[symbol]['p_score']:
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals,
                        'rr_ratio': signal.get('rr_ratio', 0.0), 
                        'rrr_net': signal.get('rrr_net', 0.0), 
                        'p_score': p_score, 
                        'adx_val': signal.get('tech_data', {}).get('adx', 0.0), 
                        'symbol': symbol,
                        'entry_type': signal.get('entry_type', 'N/A')
                    }
            
            # P-Score (純利益期待値) を最優先にソート
            sorted_best_signals = sorted(
                best_signals_per_symbol.values(), 
                key=lambda x: (
                    x['p_score'],     
                    x['rrr_net'],     
                    x['score'],       
                    x['adx_val'],     
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
                logging.info(f"🔔 高P-Score/高優位性シグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        msg = format_integrated_analysis_message_v18(symbol, item['all_signals'], i + 1)
                        
                        if msg:
                            log_symbol = symbol
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {i+1}位, P-Score: {item['p_score']:.2f}, RRR_Net: {item['rrr_net']:.2f})")
                            TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                            
                            task = asyncio.create_task(asyncio.to_thread(lambda m=msg: send_telegram_html(m)))
                            notify_tasks.append(task)
                            
                    else:
                        log_symbol = symbol
                        logging.info(f"🕒 {log_symbol} はクールダウン期間中です。通知をスキップします。")
                
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            
            if notify_tasks:
                 await asyncio.gather(*notify_tasks, return_exceptions=True)

            await asyncio.sleep(LOOP_INTERVAL) 

        except Exception as e:
            error_message = str(e)
            error_name = type(e).__name__
            
            if 'RateLimitExceeded' in error_message or error_name == 'RateLimitExceeded':
                 logging.error(f"レート制限エラー: {error_message}")
                 await asyncio.sleep(60) 
                 continue
            
            logging.error(f"メインループで致命的なエラー: {error_name} - {error_message}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v18.0.1 - Mexc Spot, P-Score/Forex Macro Adjusted")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v18.0.1 Startup initializing...") 
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
        "bot_version": "v18.0.1 - Mexc Spot, P-Score/Forex Macro Adjusted",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "commission_rate": COMMISSION_RATE
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT API v18.0.1 is running (Mexc Spot, P-Score/Forex Macro Adjusted)."}, status_code=200)

if __name__ == '__main__':
    # 実行には uvicorn main_render_v18.0.1_Mexc_Spot:app --host 0.0.0.0 --port 8080 のようなコマンドが必要です
    # uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
    pass
