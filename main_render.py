# ====================================================================================
# Apex BOT v18.0.5 - MACD/Macro Stability Fix
# 
# 修正ポイント:
# 1. 【MACD KeyError修正】analyze_single_timeframe関数内でMACDHの列が存在しない場合にKeyErrorが発生する問題を修正。
# 2. 【yfinance安定化】yfinance呼び出しから、エラーの原因となっていたXAUUSD=Xを除外。
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
SIGNAL_THRESHOLD = 0.75             # 閾値を 0.75 に設定
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

# 💡【移動平均線】長期SMAの長さ (4h足で使用)
LONG_TERM_SMA_LENGTH = 50           
# 💡【移動平均線】長期トレンド逆行時のペナルティ
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

# Dynamic Trailing Stop (DTS) Parameters
ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

# Funding Rate Bias Filter Parameters
FUNDING_RATE_THRESHOLD = 0.00015    
FUNDING_RATE_BONUS_PENALTY = 0.08   

# Dominance Bias Filter Parameters
DOMINANCE_BIAS_BONUS_PENALTY = 0.05 # BTCドミナンスの偏りによる最大ボーナス/ペナルティ点

# 💡【ファンダメンタルズ】流動性/スマートマネーフィルター Parameters
LIQUIDITY_BONUS_POINT = 0.06        # 板の厚みによるボーナス
ORDER_BOOK_DEPTH_LEVELS = 5         # オーダーブックのチェック深度
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致によるボーナス
# 💡【恐怖指数】FGIプロキシボーナス (強いリスクオン/オフの場合)
FGI_PROXY_BONUS_MAX = 0.07          

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
ORDER_BOOK_CACHE: Dict[str, Any] = {} # オーダーブックキャッシュ

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
    """TelegramにHTML形式でメッセージを送信する (ログ強化 & タイムアウト設定版)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        # 💡 修正点: timeout=10 を追加し、最大10秒でタイムアウトするようにする
        response = requests.post(url, data=payload, timeout=10) 
        response.raise_for_status() 
        logging.info("Telegram通知を正常に送信しました。") 
        return True
    except requests.exceptions.HTTPError as e:
        error_details = e.response.text if e.response else 'No detailed response'
        # ステータスコードと詳細情報を追加
        logging.error(f"Telegram HTTP Error: {e.response.status_code if e.response else 'N/A'} - {error_details}") 
        return False
    except requests.exceptions.Timeout as e:
        # 💡 新規捕捉: タイムアウトエラー専用のログ
        logging.error(f"Telegram HTTP Error: Timeout occurred (10s) - {e}")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram Request Error: {e}")
        return False

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間軸に基づき推定勝率を算出する (0.0 - 1.0 スケールで計算)"""
    adjusted_rate = 0.50 + (score - 0.50) * 1.45 
    return max(0.40, min(0.85, adjusted_rate))


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    【v17.1.3 改良版】現在単価、長期トレンド、ファンダ/恐怖指数情報に加え、初期TP目標価格を追加した通知メッセージを生成する
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral", "DataShortage (MACD)"]] # 💡 MACDエラーも除外
    if not valid_signals:
        return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals:
        return "" 
        
    # 最もスコアが高いシグナルをベストシグナルとして採用
    best_signal = max(
        high_score_signals, 
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )
    
    # ----------------------------------------------------
    # 1. 主要な取引情報の抽出
    # ----------------------------------------------------
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    side = best_signal.get('side', 'N/A')
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    tp1_price = best_signal.get('tp1', 0.0) # 💡 TP1価格の抽出
    entry_type = best_signal.get('entry_type', 'N/A')
    
    display_symbol = symbol.replace('-', '/')
    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    time_to_tp = get_tp_reach_time(timeframe)
    
    # 信頼度のテキスト表現
    if score_raw >= 0.85:
        confidence_text = "<b>極めて高い</b>"
    elif score_raw >= 0.75:
        confidence_text = "<b>高い</b>"
    else:
        confidence_text = "中程度"
        
    # 方向の絵文字とテキスト
    if side == "ロング":
        direction_emoji = "🚀"
        direction_text = "<b>ロング (LONG)</b>"
    else:
        direction_emoji = "💥"
        direction_text = "<b>ショート (SHORT)</b>"
        
    # 順位の絵文字
    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    rank_emoji = rank_emojis.get(rank, "🏆")

    # ----------------------------------------------------
    # 2. メッセージの組み立て
    # ----------------------------------------------------

    # --- ヘッダー部 (現在単価を含む) ---
    header = (
        f"{rank_emoji} <b>Apex Signal - Rank {rank}</b> {rank_emoji}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"{display_symbol} | {direction_emoji} {direction_text}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # --- 取引計画部 (TP Targetを追加) ---
    sl_width = abs(entry_price - sl_price)
    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 ATR バッファ"
        
    trade_plan = (
        f"<b>✅ 取引計画 (エントリー推奨)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>エントリー種別</b>: <code>{entry_type}</code> (指値/成行)\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>損切り (SL)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>リスク (SL幅)</b>: <code>${format_price_utility(sl_width, symbol)}</code>\n"
        f"  - <b>初期利益目標 (TP Target)</b>: <code>${format_price_utility(tp1_price, symbol)}</code> (動的追跡開始点)\n" # 💡 新規追加
        f"  - <b>目標RRR (DTS Base)</b>: 1 : {rr_ratio:.2f}+\n\n"
    )

    # --- 分析サマリー部 ---
    tech_data = best_signal.get('tech_data', {})
    regime = "トレンド相場" if tech_data.get('adx', 0.0) >= ADX_TREND_THRESHOLD else "レンジ相場"
    fgi_score = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_score > 0 else ("リスクオフ" if fgi_score < 0 else "中立")
    
    summary = (
        f"<b>💡 分析サマリー</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>分析スコア</b>: <code>{score_100:.2f} / 100</code> (信頼度: {confidence_text})\n"
        f"  - <b>予測勝率</b>: <code>約 {win_rate:.1f}%</code>\n"
        f"  - <b>時間軸 (メイン)</b>: <code>{timeframe}</code>\n"
        f"  - <b>決済までの目安</b>: {time_to_tp}\n"
        f"  - <b>市場の状況</b>: {regime} (ADX: {tech_data.get('adx', 0.0):.1f})\n"
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n\n" # 恐怖指数情報を追加
    )

    # --- 分析の根拠部 (チェックリスト形式) ---
    long_term_trend_ok = not tech_data.get('long_term_reversal_penalty', False)
    momentum_ok = tech_data.get('macd_cross_valid', True) and not tech_data.get('stoch_filter_penalty', 0) > 0
    structure_ok = tech_data.get('structural_pivot_bonus', 0.0) > 0
    volume_confirm_ok = tech_data.get('volume_confirmation_bonus', 0.0) > 0
    obv_confirm_ok = tech_data.get('obv_momentum_bonus_value', 0.0) > 0
    liquidity_ok = tech_data.get('liquidity_bonus_value', 0.0) > 0
    funding_rate_ok = tech_data.get('funding_rate_bonus_value', 0.0) > 0
    dominance_ok = tech_data.get('dominance_bias_bonus_value', 0.0) > 0
    fib_level = tech_data.get('fib_proximity_level', 'N/A')
    
    # 💡 長期トレンドの表示を強化
    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    lt_trend_check_text = f"長期 ({lt_trend_str}, SMA {LONG_TERM_SMA_LENGTH}) トレンドと一致"
    lt_trend_check_text_penalty = f"長期トレンド ({lt_trend_str}) と逆行 ({tech_data.get('long_term_reversal_penalty_value', 0.0)*100:.1f}点ペナルティ)"
    
    
    analysis_details = (
        f"<b>🔍 分析の根拠</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>トレンド/勢い</b>: \n"
        f"    {'✅' if long_term_trend_ok else '❌'} {'<b>' if not long_term_trend_ok else ''}{lt_trend_check_text if long_term_trend_ok else lt_trend_check_text_penalty}{'</b>' if not long_term_trend_ok else ''}\n" # 長期トレンド表示の強化
        f"    {'✅' if momentum_ok else '⚠️'} 短期モメンタム加速 (RSI/MACD/CCI)\n"
        f"  - <b>価格構造/ファンダ</b>: \n"
        f"    {'✅' if structure_ok else '❌'} 重要支持/抵抗線に近接 ({fib_level}確認)\n"
        f"    {'✅' if (volume_confirm_ok or obv_confirm_ok) else '❌'} 出来高/OBVの裏付け\n"
        f"    {'✅' if liquidity_ok else '❌'} 板の厚み (流動性) 優位\n"
        f"  - <b>市場心理/その他</b>: \n"
        f"    {'✅' if funding_rate_ok else '❌'} 資金調達率が有利\n"
        f"    {'✅' if dominance_ok else '❌'} BTCドミナンスが追い風\n"
    )
    
    # --- フッター部 ---
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ Limit注文は、指定水準到達時のみ約定します。DTS戦略により、SLは自動的に追跡され利益を最大化します。</pre>"
        f"<i>Bot Ver: v18.0.5 (MACD/Macro Stability Fix)</i>"
    )

    return header + trade_plan + summary + analysis_details + footer


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

async def fetch_funding_rate(symbol: str) -> float:
    """OKXからシンボルの直近の資金調達率を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
    try:
        funding_rate = await EXCHANGE_CLIENT.fetch_funding_rate(symbol)
        return funding_rate.get('fundingRate', 0.0) if funding_rate else 0.0
    except Exception as e:
        return 0.0

async def fetch_order_book_depth(symbol: str) -> Optional[Dict]:
    """OKXからオーダーブックを取得し、流動性 (上位板の厚み) を計算"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE

    # キャッシュをチェック (短時間で再利用)
    if symbol in ORDER_BOOK_CACHE and (time.time() - ORDER_BOOK_CACHE[symbol]['timestamp'] < LOOP_INTERVAL * 0.2):
         return ORDER_BOOK_CACHE[symbol]['data']
         
    if not EXCHANGE_CLIENT:
        return None
    
    try:
        # Limit 50 (TOP_SYMBOL_LIMIT * 2) で取得
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=50) 
        
        # 1. 現在価格とスプレッド
        if not orderbook['bids'] or not orderbook['asks']:
             return None
             
        best_bid_price = orderbook['bids'][0][0]
        best_ask_price = orderbook['asks'][0][0]
        current_price = (best_bid_price + best_ask_price) / 2
        
        # 2. 上位 N レベルの板の厚みを計算
        total_bid_volume = sum(orderbook['bids'][i][1] * orderbook['bids'][i][0] for i in range(min(ORDER_BOOK_DEPTH_LEVELS, len(orderbook['bids']))))
        total_ask_volume = sum(orderbook['asks'][i][1] * orderbook['asks'][i][0] for i in range(min(ORDER_BOOK_DEPTH_LEVELS, len(orderbook['asks']))))
        
        # 3. Ask/Bid比率
        ask_bid_ratio = total_ask_volume / total_bid_volume if total_bid_volume > 0 else 0.0
        
        data = {
            'price': current_price,
            'ask_volume': total_ask_volume,
            'bid_volume': total_bid_volume,
            'ask_bid_ratio': ask_bid_ratio
        }
        
        # キャッシュに保存
        ORDER_BOOK_CACHE[symbol] = {'timestamp': time.time(), 'data': data}
        
        return data

    except Exception as e:
        # logging.warning(f"オーダーブック取得エラー for {symbol}: {e}")
        return None


async def update_symbols_by_volume():
    """CCXTを使用してOKXの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
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
        
        new_monitor_symbols = [convert_symbol_to_okx_swap(symbol) for symbol, _ in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
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
        # 💡 instTypeをSWAPで取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit, params={'instType': 'SWAP'})
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except ccxt.NetworkError as e:
        return [], "ExchangeError", client_name
    except ccxt.ExchangeError as e:
        if 'market symbol' in str(e) or 'not found' in str(e):
             # スワップが見つからない場合は、現物 (SPOT) を試す
             spot_symbol = symbol.replace('-', '/')
             try:
                 ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(spot_symbol, timeframe, limit=limit, params={'instType': 'SPOT'})
                 if not ohlcv or len(ohlcv) < 30: 
                     return [], "DataShortage", client_name
                 return ohlcv, "Success", client_name
             except Exception:
                 return [], "ExchangeError", client_name
        
        return [], "ExchangeError", client_name
        
    except Exception as e:
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """
    💡【恐怖指数/移動平均線】マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend, Dominance Bias)
    """
    
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC-USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH-USDT", '4h')
    
    btc_trend = 0
    eth_trend = 0
    btc_change = 0.0
    eth_change = 0.0
    
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()

    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        # 💡 SMA 50で長期トレンドを計算
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH) 
        df_btc.dropna(subset=['sma'], inplace=True)
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]: btc_trend = 1 # Long
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]: btc_trend = -1 # Short
            if len(df_btc) >= 2:
                btc_change = (df_btc['close'].iloc[-1] - df_btc['close'].iloc[-2]) / df_btc['close'].iloc[-2]
    
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        # 💡 SMA 50で長期トレンドを計算
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]: eth_trend = 1 # Long
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]: eth_trend = -1 # Short
            if len(df_eth) >= 2:
                eth_change = (df_eth['close'].iloc[-1] - df_eth['close'].iloc[-2]) / df_eth['close'].iloc[-2]

    # 2. 💡【恐怖指数】FGI Proxyの計算 (恐怖指数/市場センチメント)
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = FGI_PROXY_BONUS_MAX # 強いリスクオン (ボーナス強化)
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -FGI_PROXY_BONUS_MAX # 強いリスクオフ (恐怖) (ボーナス強化)
        
    # 3. BTC Dominance Proxyの計算
    dominance_trend = "Neutral"
    dominance_bias_score = 0.0
    DOM_DIFF_THRESHOLD = 0.002 # 0.2%以上の差でドミナンスに偏りありと判断
    
    if status_btc == "Success" and status_eth == "Success" and len(df_btc) >= 2 and len(df_eth) >= 2:
        if btc_change - eth_change > DOM_DIFF_THRESHOLD:
            # BTCの上昇/下落がETHより強い -> ドミナンス増 (BTC強、Alt弱)
            dominance_trend = "Increasing"
            dominance_bias_score = -DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Longにペナルティ
        elif eth_change - btc_change > DOM_DIFF_THRESHOLD:
            # ETHの上昇/下落がBTCより強い -> ドミナンス減 (Alt強、BTC弱)
            dominance_trend = "Decreasing"
            dominance_bias_score = DOMINANCE_BIAS_BONUS_PENALTY # Altcoin Longにボーナス
    
    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        "dominance_trend": dominance_trend, 
        "dominance_bias_score": dominance_bias_score 
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
    
    # フィボナッチ係数
    R1 = P + (H - L) * 0.382
    S1 = P - (H - L) * 0.382
    R2 = P + (H - L) * 0.618
    S2 = P - (H - L) * 0.618
    
    return {'P': P, 'R1': R1, 'S1': S1, 'R2': R2, 'S2': S2}

def analyze_structural_proximity(price: float, pivots: Dict, side: str, atr_val: float) -> Tuple[float, float, float, str]:
    """ 価格とPivotポイントを比較し、構造的なSL/TPを決定し、ボーナススコアを返す (0.07点)
    返り値: (ボーナススコア, 構造的SL, 構造的TP, 近接Fibレベル)
    """
    
    bonus = 0.0
    structural_sl = 0.0
    structural_tp = 0.0
    BONUS_POINT = 0.07
    FIB_PROXIMITY_THRESHOLD = atr_val * 1.0 # 1 ATR以内にフィボナッチレベルがあるかチェック
    proximity_level = 'N/A'
    
    R1 = pivots.get('R1', np.nan)
    S1 = pivots.get('S1', np.nan)
    R2 = pivots.get('R2', np.nan)
    S2 = pivots.get('S2', np.nan)
    
    if pd.isna(R1) or pd.isna(S1):
        return 0.0, 0.0, 0.0, proximity_level

    # 構造的なSL/TPの採用
    if side == "ロング":
        # 1. フィボナッチレベルの近接度チェック (エントリー精度向上)
        if abs(price - S1) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'S1'
        elif abs(price - S2) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'S2'

        if price > S1 and (price - S1) / (R1 - S1) < 0.5: # Current price is between S1 and the midpoint (closer to S1). Idea: Target R1, SL S1.
            bonus = BONUS_POINT
            structural_sl = S1
            structural_tp = R1 * 1.01
        elif price > R1: # Price is above R1 (Breakout scenario). Idea: Target R1 extension, SL R1.
            bonus = BONUS_POINT
            structural_sl = R1
            structural_tp = R1 * 1.05

    elif side == "ショート":
        # 1. フィボナッチレベルの近接度チェック (エントリー精度向上)
        if abs(price - R1) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R1'
        elif abs(price - R2) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R2'

        if price < R1 and (R1 - price) / (R1 - S1) < 0.5: # Current price is between R1 and the midpoint (closer to R1). Idea: Target S1, SL R1.
            bonus = BONUS_POINT
            structural_sl = R1 # R1 is the resistance used as SL
            structural_tp = S1 * 0.99
        elif price < S1: # Price is below S1 (Breakout scenario). Idea: Target S1 extension, SL S1.
            bonus = BONUS_POINT
            structural_sl = S1 # S1 is the support used as SL/resistance
            structural_tp = S1 * 0.95

    # 近接レベルにいる場合はボーナスを強化
    if proximity_level != 'N/A':
        bonus = max(bonus, BONUS_POINT + 0.03)

    return bonus, structural_sl, structural_tp, proximity_level


async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """ 単一の時間軸で分析とシグナル生成を行う関数 (v18.0.5) """
    
    # 1. データ取得とFunding Rate/Order Book取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    funding_rate_val = 0.0
    order_book_data = None
    
    if timeframe == '1h':
        funding_rate_val = await fetch_funding_rate(symbol)
        # 💡 オーダーブック深度を取得
        order_book_data = await fetch_order_book_depth(symbol)

    if status != "Success":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.00, 'rrr_net': 0.00}

    # 2. DataFrameの準備
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df.set_index(pd.to_datetime(df['timestamp'], unit='ms'), inplace=True)
    
    if df.empty or len(df) < 40:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.00, 'rrr_net': 0.00}
    
    # 3. テクニカル指標の計算 (pandas_ta)
    
    # 出来高確認: 直近の出来高が過去の平均出来高と比較して高いか
    df['VOL_SMA'] = ta.sma(df['volume'], length=10)
    df['VOL_CONFIRM'] = df['volume'] > (df['VOL_SMA'] * VOLUME_CONFIRMATION_MULTIPLIER)

    # 💡 OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # ATR (Trailing Stop / SL設定用)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['ATR_TRAIL'] = ta.supertrend(df['high'], df['low'], df['close'], length=10, multiplier=ATR_TRAIL_MULTIPLIER)['SUPERT_10_3.0'] 

    # モメンタム指標
    df['RSI'] = ta.rsi(df['close'], length=14)
    df['CCI'] = ta.cci(df['high'], df['low'], df['close'], length=20)
    df['ADX'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
    
    # 移動平均線/トレンド
    df['SMA20'] = ta.sma(df['close'], length=20)
    df['SMA50'] = ta.sma(df['close'], length=50) 
    
    # MACDクロス
    macd_data = ta.macd(df['close'])
    
    # 💡 修正: MACDH_12_26_9 の KeyError 対策
    macd_key = 'MACD_12_26_9'
    macdh_key = 'MACDH_12_26_9'
    macds_key = 'MACDS_12_26_9'
    
    if macd_key not in macd_data.columns or macdh_key not in macd_data.columns or macds_key not in macd_data.columns:
        # MACDデータが不完全な場合は、この時間軸の分析をスキップする
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage (MACD)', 'score': 0.00, 'rrr_net': 0.00}

    df['MACD'] = macd_data[macd_key]
    df['MACDH'] = macd_data[macdh_key]
    df['MACD_SIG'] = macd_data[macds_key]
    
    df['MACD_CROSS_UP'] = (df['MACD'].shift(1) < df['MACD_SIG'].shift(1)) & (df['MACD'] >= df['MACD_SIG'])
    df['MACD_CROSS_DOWN'] = (df['MACD'].shift(1) > df['MACD_SIG'].shift(1)) & (df['MACD'] <= df['MACD_SIG'])

    # ボリンジャーバンド (Volatility Check)
    bbands = ta.bbands(df['close'], length=20, std=2)
    df['BBL'] = bbands['BBL_20_2.0']
    df['BBU'] = bbands['BBU_20_2.0']

    # フィボナッチ・ピボット
    pivots = calculate_fib_pivot(df)

    # 最後の行にインデックスを設定
    last_row = df.iloc[-1]
    
    # 4. シグナルスコアの計算
    
    current_price = last_row['close']
    current_atr = last_row['ATR']
    
    # スコアリング初期化 (基本スコア)
    score = BASE_SCORE
    side = "Neutral"
    
    # --- スコアリング変数 ---
    volume_confirmation_bonus = 0.0
    structural_pivot_bonus = 0.0
    liquidity_bonus_value = 0.0
    funding_rate_bonus_value = 0.0
    dominance_bias_bonus_value = 0.0
    obv_momentum_bonus_value = 0.0
    sentiment_fgi_proxy_bonus = macro_context.get('sentiment_fgi_proxy', 0.0) # FGIプロキシスコア
    long_term_reversal_penalty_value = 0.0
    stoch_filter_penalty = 0.0 # ストキャスティクスによる過熱ペナルティ
    
    
    # --- 総合トレンド判断 ---
    
    # RSI: 過売/過買ゾーンからの脱却
    is_bullish_momentum = last_row['RSI'] > RSI_MOMENTUM_HIGH
    is_bearish_momentum = last_row['RSI'] < RSI_MOMENTUM_LOW
    
    # MACD: クロス確認
    macd_cross_valid = True
    
    # CCI: 極端な過熱感のチェック (ペナルティ適用)
    if last_row['CCI'] > CCI_OVERBOUGHT * 2 or last_row['CCI'] < CCI_OVERSOLD * 2:
        stoch_filter_penalty = 0.15 # 非常に過熱している場合はペナルティを強化
    
    # Long Term Trend (4h足での長期トレンドと一致しない場合はペナルティ)
    current_trend_str = "Neutral"
    if last_row['close'] > last_row['SMA50']:
        current_trend_str = "Long"
    elif last_row['close'] < last_row['SMA50']:
        current_trend_str = "Short"
    
    if long_term_trend != 'Neutral' and current_trend_str != long_term_trend:
         # 長期足で上昇、短期足で下降トレンドなど、逆行している場合は強力なペナルティ
         score -= LONG_TERM_REVERSAL_PENALTY 
         long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
         long_term_penalty_applied = True
         
    # 出来高の確認
    if last_row['VOL_CONFIRM']:
        volume_confirmation_bonus = 0.05
    
    # --- シグナル候補の決定 ---
    
    # 1. MACDゴールデンクロス、またはRSIの過売ゾーンからの反転
    if last_row['MACD_CROSS_UP'] or (last_row['RSI'] >= RSI_MOMENTUM_LOW and df['RSI'].iloc[-2] < RSI_MOMENTUM_LOW):
        side = "ロング"
        score += volume_confirmation_bonus
        if last_row['MACD_CROSS_UP']:
            score += 0.05
        if last_row['RSI'] < RSI_OVERSOLD:
            score += 0.05 # 過売からの脱却はボーナス
            
        # 💡 OBVトレンド確認: OBVがSMAを上回っているか
        if last_row['OBV'] > last_row['OBV_SMA']:
            score += OBV_MOMENTUM_BONUS
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
            
        # 💡 MACDデッドクロスによるペナルティ
        if last_row['MACD_CROSS_DOWN']:
             score -= MACD_CROSS_PENALTY
             macd_cross_valid = False

    # 2. MACDデッドクロス、またはRSIの過買ゾーンからの反転
    elif last_row['MACD_CROSS_DOWN'] or (last_row['RSI'] <= RSI_MOMENTUM_HIGH and df['RSI'].iloc[-2] > RSI_MOMENTUM_HIGH):
        side = "ショート"
        score += volume_confirmation_bonus
        if last_row['MACD_CROSS_DOWN']:
            score += 0.05
        if last_row['RSI'] > RSI_OVERBOUGHT:
            score += 0.05 # 過買からの脱却はボーナス
            
        # 💡 OBVトレンド確認: OBVがSMAを下回っているか
        if last_row['OBV'] < last_row['OBV_SMA']:
            score += OBV_MOMENTUM_BONUS
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
            
        # 💡 MACDゴールデンクロスによるペナルティ
        if last_row['MACD_CROSS_UP']:
             score -= MACD_CROSS_PENALTY
             macd_cross_valid = False

    # シグナルがない場合は終了
    if side == "Neutral":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'Neutral', 'score': 0.00, 'rrr_net': 0.00}
        
    # --- 共通のボーナス/ペナルティ適用 ---

    # 構造的ボーナス (フィボナッチ/ピボット)
    pivot_bonus, structural_sl, structural_tp, fib_level = analyze_structural_proximity(current_price, pivots, side, current_atr)
    score += pivot_bonus
    structural_pivot_bonus = pivot_bonus
    structural_sl_used = structural_sl != 0.0 # 構造的SLが設定されたか

    # Volatility Filter (極端な収縮または拡散はペナルティ)
    bb_width_percent = (last_row['BBU'] - last_row['BBL']) / last_row['close'] * 100
    if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
        score -= 0.10 # 高すぎるボラティリティはペナルティ
    
    # Funding Rate Filter (1h足のみ)
    if timeframe == '1h' and abs(funding_rate_val) > FUNDING_RATE_THRESHOLD:
        if (side == "ロング" and funding_rate_val < 0) or (side == "ショート" and funding_rate_val > 0):
            score += FUNDING_RATE_BONUS_PENALTY # 資金調達率が有利な方向
            funding_rate_bonus_value = FUNDING_RATE_BONUS_PENALTY
        else:
            score -= FUNDING_RATE_BONUS_PENALTY
            funding_rate_bonus_value = -FUNDING_RATE_BONUS_PENALTY

    # 💡 流動性/オーダーブックの確認 (1h足のみ)
    if timeframe == '1h' and order_book_data:
        ratio = order_book_data.get('ask_bid_ratio', 1.0)
        
        if side == "ロング":
            if ratio < 1.0: # 買い板が厚い (Ask/Bid < 1.0)
                score += LIQUIDITY_BONUS_POINT
                liquidity_bonus_value = LIQUIDITY_BONUS_POINT
        elif side == "ショート":
            if ratio > 1.0: # 売り板が厚い (Ask/Bid > 1.0)
                score += LIQUIDITY_BONUS_POINT
                liquidity_bonus_value = LIQUIDITY_BONUS_POINT
                
    # 💡 FGI Proxy/市場センチメントの適用
    if (side == "ロング" and sentiment_fgi_proxy_bonus > 0) or (side == "ショート" and sentiment_fgi_proxy_bonus < 0):
         score += abs(sentiment_fgi_proxy_bonus)
    elif sentiment_fgi_proxy_bonus != 0:
         score -= abs(sentiment_fgi_proxy_bonus) # 市場センチメントと逆行する場合はペナルティ

    # 💡 BTC Dominance Bias (Altcoin Longの場合のみ適用)
    if symbol != 'BTC-USDT' and side == "ロング":
        dominance_bias_score = macro_context.get('dominance_bias_score', 0.0)
        score += dominance_bias_score
        dominance_bias_bonus_value = dominance_bias_score
        
    # 💡 極端な過熱感ペナルティを適用
    score -= stoch_filter_penalty
    
    # 5. エントリー・SL・TPの計算 (DTS戦略)
    
    # SL (SL幅はATR * Multiplierが基本。構造的SLがあればそちらを優先し、0.5 ATRのバッファを追加)
    sl_distance = current_atr * ATR_TRAIL_MULTIPLIER
    
    if structural_sl_used:
        # 構造的SLを使用する場合
        if side == "ロング":
            sl_price = structural_sl - (current_atr * 0.5) 
        else: # ショート
            sl_price = structural_sl + (current_atr * 0.5)
            
    else:
        # ATR Trailing SLを使用する場合
        if side == "ロング":
            sl_price = current_price - sl_distance
        else: # ショート
            sl_price = current_price + sl_distance
            
    # TP1 (RRR: 1:DTS_RRR_DISPLAY を目標とする初期TP)
    rr_target_distance = abs(current_price - sl_price) * DTS_RRR_DISPLAY
    if side == "ロング":
        tp1_price = current_price + rr_target_distance
    else: # ショート
        tp1_price = current_price - rr_target_distance

    # RRR Netの計算 (SLからTP1までのRRR)
    risk = abs(current_price - sl_price)
    reward = abs(tp1_price - current_price)
    rr_ratio = reward / risk if risk > 0 else 0.0
    
    # 6. 結果の格納 (技術データ詳細を含む)
    
    final_score = max(0.01, min(0.99, score))
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'rr_ratio': rr_ratio, # TP1までのRRR
        'rrr_net': final_score * rr_ratio, # 総合優位性スコア (P-Score * RRR)
        'price': current_price,
        'entry': current_price, 
        'sl': sl_price,
        'tp1': tp1_price, # 💡 TP1を格納
        'entry_type': 'Market/Limit',
        'tech_data': {
            'atr': current_atr,
            'adx': last_row['ADX'],
            'rsi': last_row['RSI'],
            'macd_cross_valid': macd_cross_valid,
            'structural_pivot_bonus': structural_pivot_bonus,
            'structural_sl_used': structural_sl_used,
            'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
            'liquidity_bonus_value': liquidity_bonus_value,
            'funding_rate_bonus_value': funding_rate_bonus_value,
            'dominance_bias_bonus_value': dominance_bias_bonus_value,
            'obv_momentum_bonus_value': obv_momentum_bonus_value,
            'long_term_trend': long_term_trend,
            'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
            'stoch_filter_penalty': stoch_filter_penalty,
            'volume_confirmation_bonus': volume_confirmation_bonus,
            'fib_proximity_level': fib_level
        }
    }


async def run_multi_timeframe_analysis(symbol: str, macro_context: Dict) -> List[Dict]:
    """ 複数時間足 (15m, 1h, 4h) で分析を実行し、有効なシグナルを返す """
    
    timeframes = ['15m', '1h', '4h']
    tasks = []
    signals: List[Dict] = []
    
    # 長期トレンド情報を先に取得 (4h)
    ohlcv_4h, status_4h, client_used_4h = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '4h')
    
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce').astype('float64')
    df_4h['SMA50'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH) 
    
    long_term_trend = "Neutral"
    long_term_penalty_applied = False
    
    if not df_4h.empty and len(df_4h) >= LONG_TERM_SMA_LENGTH:
        last_4h_close = df_4h['close'].iloc[-1]
        last_4h_sma = df_4h['SMA50'].iloc[-1]
        
        if last_4h_close > last_4h_sma:
            long_term_trend = "Long"
        elif last_4h_close < last_4h_sma:
            long_term_trend = "Short"
            
    # 各時間足のタスクを作成
    for tf in timeframes:
        # 1h以下の足は、長期トレンドが逆行している場合はペナルティを適用する
        tasks.append(
            analyze_single_timeframe(
                symbol, tf, macro_context, CCXT_CLIENT_NAME, long_term_trend, long_term_penalty_applied
            )
        )
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) # レートリミット回避のための遅延
        
    results = await asyncio.gather(*tasks)
    
    for result in results:
        if result and result.get('side') not in ["DataShortage", "ExchangeError", "Neutral", "DataShortage (MACD)"]:
            signals.append(result)

    return signals


# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def notify_signals_in_queue():
    """通知キューにあるシグナルをチェックし、クールダウンを考慮して通知する"""
    global LAST_ANALYSIS_SIGNALS, TRADE_NOTIFIED_SYMBOLS
    
    if not LAST_ANALYSIS_SIGNALS:
        return

    # 総合優位性スコア (P-Score * RRR) でソートし、閾値以上のものを抽出
    high_value_signals = sorted(
        [s for s in LAST_ANALYSIS_SIGNALS if s.get('rrr_net', 0.0) >= (SIGNAL_THRESHOLD * 0.8)], # RRR_NETを考慮した閾値
        key=lambda x: x.get('rrr_net', 0.0), 
        reverse=True
    )

    notified_count = 0
    now = time.time()
    
    logging.info(f"🔔 高P-Score/高優位性シグナル {len(high_value_signals)} 銘柄をチェックします。")

    for rank, signal in enumerate(high_value_signals, 1):
        symbol = signal['symbol']
        timeframe = signal['timeframe']
        
        # クールダウンチェック
        if symbol in TRADE_NOTIFIED_SYMBOLS and (now - TRADE_NOTIFIED_SYMBOLS[symbol]) < TRADE_SIGNAL_COOLDOWN:
            elapsed_time = now - TRADE_NOTIFIED_SYMBOLS[symbol]
            remaining_time = TRADE_SIGNAL_COOLDOWN - elapsed_time
            logging.info(f"🕒 {symbol} はクールダウン期間中です (残り {remaining_time:.0f} 秒)。通知をスキップします。")
            continue
            
        # スコアとRRR_Netのログ出力（P-Score 0.00のログ問題を修正するため、実際の値をログに出力）
        p_score = signal.get('score', 0.00)
        rrr_net = signal.get('rrr_net', 0.00)
        logging.info(f"📰 通知タスクをキューに追加: {symbol} (順位: {rank}位, P-Score: {p_score:.2f}, RRR_Net: {rrr_net:.2f})")

        # 通知メッセージの生成
        message = format_integrated_analysis_message(symbol, [s for s in LAST_ANALYSIS_SIGNALS if s['symbol'] == symbol], rank)
        
        if message:
            # Telegram通知
            success = send_telegram_html(message)
            
            if success:
                TRADE_NOTIFIED_SYMBOLS[symbol] = now
                notified_count += 1
                if notified_count >= TOP_SIGNAL_COUNT:
                    break
            
            await asyncio.sleep(1.0) # Telegram APIレートリミット回避

async def main_loop():
    """ボットのメイン実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT
    
    # CCXT初期化
    await initialize_ccxt_client()

    # 為替/マクロコンテキストの初期化 (yfinanceによる為替データ取得を含む)
    try:
        # 💡 yfinanceによるFXデータ取得
        # 修正: XAUUSD=X はデータ欠落エラーが頻発するため除外
        tickers = ['JPY=X', 'EURUSD=X'] 
        FX_DATA_PERIOD = "5d"
        FX_DATA_INTERVAL = "1h"
        # yfinanceのFutureWarningを回避
        fx_data_multi = yf.download(tickers, period=FX_DATA_PERIOD, interval=FX_DATA_INTERVAL, progress=False, ignore_tz=True)['Close']
        
        # 💡 為替/マクロコンテキストの判断ロジック (簡略化)
        macro_bias = 0.0
        macro_text = "Neutral"
        
        if not fx_data_multi.empty and 'JPY=X' in fx_data_multi.columns:
             jpy_change = (fx_data_multi['JPY=X'].iloc[-1] - fx_data_multi['JPY=X'].iloc[-2]) / fx_data_multi['JPY=X'].iloc[-2]
             
             if jpy_change > 0.005: # 円安 (リスクオン)
                 macro_bias = 0.035
                 macro_text = "Risk-On (JPY Weakness)"
             elif jpy_change < -0.005: # 円高 (リスクオフ)
                 macro_bias = -0.035
                 macro_text = "Risk-Off (JPY Strength)"
        
        # 💡 グローバルコンテキストにFXバイアスを格納
        GLOBAL_MACRO_CONTEXT['fx_bias'] = macro_bias
        logging.info(f"🌎 為替/マクロコンテキスト: {macro_text} (Bias: {macro_bias:.4f})")
        
    except Exception as e:
        error_name = type(e).__name__
        if error_name != 'HTTPError': # yfinanceのエラーコードを抑制
             logging.error(f"yfinanceによるFXデータ取得エラー: {e}")
        GLOBAL_MACRO_CONTEXT['fx_bias'] = 0.0

    while True:
        try:
            now = time.time()
            if now - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(1)
                continue

            LAST_UPDATE_TIME = now
            
            # 1. 銘柄リストの動的更新
            await update_symbols_by_volume()
            
            # 2. マクロコンテキストの更新 (BTC Dominance, FGI Proxy)
            crypto_macro = await get_crypto_macro_context()
            GLOBAL_MACRO_CONTEXT.update(crypto_macro)
            
            # 3. Funding Rateのログ出力 (参考情報)
            btc_fr = await fetch_funding_rate("BTC-USDT")
            logging.info(f"💰 Coinglass Funding Rate: {btc_fr*100:.4f}% (BTC)") 
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(CURRENT_MONITOR_SYMBOLS[:5]).replace('-', '/')},...")

            # 4. 複数銘柄/複数時間足の並行分析
            analysis_tasks = [
                run_multi_timeframe_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            all_results: List[List[Dict]] = await asyncio.gather(*analysis_tasks)
            
            # 5. 結果を平坦化し、LAST_ANALYSIS_SIGNALSを更新
            LAST_ANALYSIS_SIGNALS = [signal for signals in all_results for signal in signals]
            
            # 6. シグナル通知の実行 (クールダウンチェックを含む)
            await notify_signals_in_queue()
            
            # 7. 成功時の状態更新
            LAST_SUCCESS_TIME = now
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except ccxt.DDoSProtection as e:
            logging.warning(f"レートリミットまたはDDoS保護がトリガーされました。{e} 60秒待機します。")
            await asyncio.sleep(60)
        except ccxt.ExchangeNotAvailable as e:
            logging.error(f"取引所が利用できません: {e} 120秒待機します。")
            await asyncio.sleep(120)
        except Exception as e:
            error_name = type(e).__name__
            try:
                # CCXTクライアントを閉じる試み
                if EXCHANGE_CLIENT:
                    await EXCHANGE_CLIENT.close()
            except:
                pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name} - {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# (バージョン更新のみ)
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v18.0.5 - MACD/Macro Stability Fix")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v18.0.5 Startup initializing...") 
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
        "bot_version": "v18.0.5 - MACD/Macro Stability Fix",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT API is running. Version: v18.0.5"})

if __name__ == '__main__':
    # 環境変数からポートを取得し、デフォルトは10000とする
    port = int(os.environ.get("PORT", 10000))
    # ローカル実行用の設定
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
