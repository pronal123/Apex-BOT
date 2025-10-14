# ====================================================================================
# Apex BOT v19.0.3 - MEXC Spot Trading Implementation (Balance Re-Hotfix)
# 
# 修正ポイント:
# 1. 🚨【致命的な再修正】CCXTのfetch_balanceエラー 'mexc fetchBalance() not support self method' に対応。
#    fetch_current_balance_usdt から params={'type': 'SPOT'} の指定を削除し、
#    クライアント初期化時の defaultType: 'spot' に依存するように変更。
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
from fastapi import FastAPI, Request 
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 
import random 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS (中略)
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
LOOP_INTERVAL = 180        
REQUEST_DELAY_PER_SYMBOL = 0.5 

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

LONG_TERM_SMA_LENGTH = 50           
LONG_TERM_REVERSAL_PENALTY = 0.20   
MACD_CROSS_PENALTY = 0.15           

ATR_TRAIL_MULTIPLIER = 3.0          
DTS_RRR_DISPLAY = 5.0               

LIQUIDITY_BONUS_POINT = 0.06        
ORDER_BOOK_DEPTH_LEVELS = 5         
OBV_MOMENTUM_BONUS = 0.04           
FGI_PROXY_BONUS_MAX = 0.07          

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               
RSI_MOMENTUM_HIGH = 60              
ADX_TREND_THRESHOLD = 30            
BASE_SCORE = 0.40                   
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 
CCI_OVERBOUGHT = 100                
CCI_OVERSOLD = -100                 

CCXT_CLIENT_NAME: str = 'MEXC' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} 
ACTUAL_POSITIONS: Dict[str, Dict] = {} 
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
HOURLY_NOTIFICATION_INTERVAL = 60 * 60 

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)

# ====================================================================================
# UTILITIES & FORMATTING (中略)
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    if timeframe == '15m': return "数時間〜半日"
    if timeframe == '1h': return "半日〜数日"
    if timeframe == '4h': return "数日〜1週間"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    if price < 0.0001: return f"{price:.8f}"
    if price < 0.01: return f"{price:.6f}"
    if price < 1.0: return f"{price:.4f}"
    if price < 100.0: return f"{price:.2f}"
    return f"{price:,.2f}"

def format_usdt(amount: float) -> str:
    return f"{amount:,.2f}"

def format_pnl(pnl: float) -> str:
    if pnl > 0: return f"🟢 +${pnl:,.2f}"
    if pnl < 0: return f"🔴 -${abs(pnl):,.2f}"
    return f"⚫️ ${pnl:,.2f}"

def calculate_pnl_utility(side: str, entry_price: float, current_price: float, amount_usdt: float, amount_coin: float) -> Tuple[float, float]:
    if amount_coin == 0 or entry_price == 0:
        return 0.0, 0.0
        
    if side == "ロング":
        pnl = amount_coin * (current_price - entry_price)
    else: 
        return 0.0, 0.0
        
    pnl_percent = (pnl / amount_usdt) * 100 if amount_usdt > 0 else 0.0
    return pnl, pnl_percent
    
def calculate_position_size(price: float, balance_usdt: float) -> Tuple[float, float]:
    POSITION_USDT_VALUE = min(balance_usdt * 0.05, 500.0) 
    if POSITION_USDT_VALUE < 10: 
         return 0.0, 0.0
         
    amount_coin = POSITION_USDT_VALUE / price
    return POSITION_USDT_VALUE, amount_coin


def send_telegram_html(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN':
        logging.warning("⚠️ TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=payload, timeout=5)
        response.raise_for_status() 
    except requests.exceptions.HTTPError as e:
        logging.error(f"Telegram HTTPエラー ({e.response.status_code}): {e.response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegramへの接続エラー: {e}")
    except Exception as e:
        logging.error(f"未知のTelegram通知エラー: {e}")
        
def get_estimated_win_rate(score: float, timeframe: str) -> float:
    base_rate = score * 0.70 + 0.30 
    
    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate))
    elif timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    elif timeframe == '4h':
        return max(0.50, min(0.90, base_rate))
    return base_rate

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    valid_signals = [s for s in signals if s.get('side') == 'ロング'] 
    if not valid_signals:
        return "" 
        
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals:
        return "" 
        
    best_signal = max(
        high_score_signals, 
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )
    
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    tp1_price = best_signal.get('tp1', 0.0) 
    entry_type = "Market/Limit" 
    
    display_symbol = symbol
    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100
    time_to_tp = get_tp_reach_time(timeframe)
    
    if score_raw >= 0.85:
        confidence_text = "<b>極めて高い</b>"
    elif score_raw >= 0.75:
        confidence_text = "<b>高い</b>"
    else:
        confidence_text = "中程度"
        
    direction_emoji = "🚀"
    direction_text = "<b>現物買い (LONG)</b>"
        
    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    rank_emoji = rank_emojis.get(rank, "🏆")

    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 ATR バッファ"
        
    header = (
        f"{rank_emoji} <b>Apex Signal - Rank {rank}</b> {rank_emoji}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<b>{display_symbol}</b> | {direction_emoji} {direction_text} (MEXC Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    trade_plan = (
        f"<b>✅ 取引計画 (現物買い/指値売り自動設定)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>エントリー種別</b>: <code>{entry_type}</code> (成行で買い)\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>自動損切り (SL 指値)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>自動利確 (TP 指値)</b>: <code>${format_price_utility(tp1_price, symbol)}</code> (動的追跡開始点)\n"
        f"  - <b>目標RRR (DTS Base)</b>: 1 : {rr_ratio:.2f}+\n\n"
    )

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
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n\n" 
    )

    long_term_trend_ok = not tech_data.get('long_term_reversal_penalty', False)
    momentum_ok = tech_data.get('macd_cross_valid', True) and not tech_data.get('stoch_filter_penalty', 0) > 0
    structure_ok = tech_data.get('structural_pivot_bonus', 0.0) > 0
    volume_confirm_ok = tech_data.get('volume_confirmation_bonus', 0.0) > 0
    obv_confirm_ok = tech_data.get('obv_momentum_bonus_value', 0.0) > 0
    liquidity_ok = tech_data.get('liquidity_bonus_value', 0.0) > 0
    fib_level = tech_data.get('fib_proximity_level', 'N/A')
    
    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    lt_trend_check_text = f"長期 ({lt_trend_str}, SMA {LONG_TERM_SMA_LENGTH}) トレンドと一致"
    lt_trend_check_text_penalty = f"長期トレンド ({lt_trend_str}) と逆行 ({tech_data.get('long_term_reversal_penalty_value', 0.0)*100:.1f}点ペナルティ)"
    
    
    analysis_details = (
        f"<b>🔍 分析の根拠</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>トレンド/勢い</b>: \n"
        f"    {'✅' if long_term_trend_ok else '❌'} {'<b>' if not long_term_trend_ok else ''}{lt_trend_check_text if long_term_trend_ok else lt_trend_check_text_penalty}{'</b>' if not long_term_trend_ok else ''}\n"
        f"    {'✅' if momentum_ok else '⚠️'} 短期モメンタム加速 (RSI/MACD/CCI)\n"
        f"  - <b>価格構造/ファンダ</b>: \n"
        f"    {'✅' if structure_ok else '❌'} 重要支持/抵抗線に近接 ({fib_level}確認)\n"
        f"    {'✅' if (volume_confirm_ok or obv_confirm_ok) else '❌'} 出来高/OBVの裏付け\n"
        f"    {'✅' if liquidity_ok else '❌'} 板の厚み (流動性) 優位\n"
    )
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ 成行買いを実行し、SL/TP指値売り注文を自動設定します。</pre>"
        f"<i>Bot Ver: v19.0.3 (Balance Re-Hotfix)</i>" 
    )

    return header + trade_plan + summary + analysis_details + footer


async def send_position_status_notification(event_type: str, new_order_info: Optional[Dict] = None):
    global ACTUAL_POSITIONS, EXCHANGE_CLIENT
    
    current_balance, positions = await get_open_spot_position()
    ACTUAL_POSITIONS = positions 
    
    if not ACTUAL_POSITIONS:
        message = (
            f"<b>📊 {event_type} - アカウントステータス</b>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - <b>現在の残高 (USDT)</b>: <code>${format_usdt(current_balance)}</code>\n"
            f"  - <b>オープンポジション</b>: <code>なし</code>\n"
            f"  - <b>取引所</b>: <code>{CCXT_CLIENT_NAME} Spot</code>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: v19.0.3 (Balance Re-Hotfix)</i>" 
        )
        send_telegram_html(message)
        return

    message = (
        f"<b>🚨 {event_type} - ポジション詳細通知</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在の残高 (USDT)</b>: <code>${format_usdt(current_balance)}</code>\n"
        f"  - <b>取引所</b>: <code>{CCXT_CLIENT_NAME} Spot</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    total_unrealized_pnl = 0.0
    
    for symbol, pos in list(ACTUAL_POSITIONS.items()):
        symbol_display = symbol
        
        latest_price = pos['entry_price'] 
        try:
             ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
             latest_price = ticker.get('last', pos['entry_price'])
        except Exception:
             pass 
             
        current_pnl, pnl_percent = calculate_pnl_utility(pos['side'], pos['entry_price'], latest_price, pos['amount_usdt'], pos['amount_coin'])
        total_unrealized_pnl += current_pnl
        
        pnl_at_tp = calculate_pnl_utility('ロング', pos['entry_price'], pos['tp1'], pos['amount_usdt'], pos['amount_coin'])[0] if pos['tp1'] > 0 else 0.0
        pnl_at_sl = calculate_pnl_utility('ロング', pos['entry_price'], pos['sl'], pos['amount_usdt'], pos['amount_coin'])[0] if pos['sl'] > 0 else 0.0
        
        sl_order_open = any(o['price'] == pos['sl'] and o['side'] == 'sell' for o in pos['open_orders'])
        tp_order_open = any(o['price'] == pos['tp1'] and o['side'] == 'sell' for o in pos['open_orders'])
        
        position_details = (
            f"\n--- {symbol_display} (ロング) ---\n"
            f"  - <b>評価損益 (P&L)</b>: {format_pnl(current_pnl)} (<code>{pnl_percent:.2f}%</code>)\n"
            f"  - <b>最新単価 (Price)</b>: <code>${format_price_utility(latest_price, symbol)}</code>\n"
            f"  - <b>エントリー単価</b>: <code>${format_price_utility(pos['entry_price'], symbol)}</code>\n"
            f"  - <b>TP 指値価格</b>: <code>${format_price_utility(pos['tp1'], symbol)}</code> ({format_pnl(pnl_at_tp)} P&L) {'✅ Open' if tp_order_open else '❌ Closed/N/A'}\n"
            f"  - <b>SL 指値価格</b>: <code>${format_price_utility(pos['sl'], symbol)}</code> ({format_pnl(pnl_at_sl)} P&L) {'✅ Open' if sl_order_open else '❌ Closed/N/A'}\n"
            f"  - <b>ポジションサイズ</b>: <code>{pos['amount_coin']:.4f} {symbol_display.split('/')[0]}</code> (${format_usdt(pos['amount_usdt'])})\n"
        )
        message += position_details

    message += (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>合計未実現損益</b>: {format_pnl(total_unrealized_pnl)}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.3 (Balance Re-Hotfix)</i>" 
    )
    
    send_telegram_html(message)


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (MEXC)"""
    global EXCHANGE_CLIENT
    
    mexc_key = os.environ.get('MEXC_API_KEY')
    mexc_secret = os.environ.get('MEXC_SECRET')
    
    config = {
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'defaultSubType': 'spot', 
        }, 
        'apiKey': mexc_key,
        'secret': mexc_secret,
    }
    
    EXCHANGE_CLIENT = ccxt_async.mexc(config) 
    
    if EXCHANGE_CLIENT:
        auth_status = "認証済み" if mexc_key and mexc_secret else "公開データのみ"
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - {auth_status}, Default: Spot)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")


# 💡 修正後の fetch_current_balance_usdt (params削除)
async def fetch_current_balance_usdt() -> float:
    """CCXTから現在のUSDT残高を取得する。失敗した場合は0を返す。"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        # 🚨 再修正: params={'type': 'SPOT'} の指定を削除。
        #           クライアント初期化時の defaultType: 'spot' に依存する
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高の存在をチェックする
        if 'USDT' not in balance:
            if any(v.get('total', 0) > 0 for k, v in balance.items()):
                 logging.warning(f"残高取得エラー: 残高情報に'USDT'キーが見つかりません。他の通貨に残高があるか、API権限設定を確認してください。")
            else:
                 logging.error(f"残高取得エラー: 残高情報に'USDT'キーが見つかりません。APIキー/権限設定を確認してください。")
            return 0.0
            
        # SpotアカウントのUSDT残高を取得 (freeまたはtotalを使用)
        usdt_free = balance['USDT'].get('free', 0.0) 
        
        return usdt_free
        
    except Exception as e:
        # APIキーがない/エラーの場合は残高0として処理
        logging.error(f"残高取得エラー（APIキー未設定/通信エラーの可能性）: {e}")
        return 0.0


async def update_symbols_by_volume():
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        await EXCHANGE_CLIENT.load_markets() 
        
        usdt_tickers = {}
        for symbol, market in EXCHANGE_CLIENT.markets.items():
            if market['active'] and market['quote'] == 'USDT' and market['spot']:
                try:
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    if ticker.get('quoteVolume') is not None:
                        usdt_tickers[symbol] = ticker
                except Exception:
                    continue 
        
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
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return [], "ExchangeError", client_name
        
    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30:
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except Exception as e:
        return [], "ExchangeError", client_name

async def get_crypto_macro_context() -> Dict:
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC/USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH/USDT", '4h')

    btc_trend = 0
    eth_trend = 0
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()

    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH)
        df_btc.dropna(subset=['sma'], inplace=True)
        
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]:
                btc_trend = 1
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]:
                btc_trend = -1

    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]:
                eth_trend = 1
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]:
                eth_trend = -1

    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = FGI_PROXY_BONUS_MAX
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -FGI_PROXY_BONUS_MAX
        
    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        'fx_bias': 0.0
    }
    
async def fetch_order_book_depth(symbol: str) -> Optional[Dict]:
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        order_book = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=ORDER_BOOK_DEPTH_LEVELS)
        
        def calculate_depth_usdt(entries: List[List[float]]) -> float:
            total_usdt = 0.0
            for price, amount in entries:
                total_usdt += price * amount
            return total_usdt

        total_bids_usdt = calculate_depth_usdt(order_book['bids'])
        total_asks_usdt = calculate_depth_usdt(order_book['asks'])
        
        ORDER_BOOK_CACHE[symbol] = {
            'bids_usdt': total_bids_usdt,
            'asks_usdt': total_asks_usdt,
            'last_updated': time.time()
        }
        return ORDER_BOOK_CACHE[symbol]
        
    except Exception as e:
        logging.warning(f"{symbol} のオーダーブック取得エラー: {e}")
        return None

# ====================================================================================
# CORE ANALYSIS LOGIC (中略)
# ====================================================================================

def analyze_structural_proximity(df: pd.DataFrame, price: float, side: str) -> Tuple[float, float, bool, str]:
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    atr = df['atr'].iloc[-1] if not df.empty and df['atr'].iloc[-1] > 0 else 0.001
    
    pivot_long = df['low'].rolling(window=20).min().iloc[-1]
    
    structural_sl_used = False
    
    if side == 'ロング':
        structural_sl_candidate = pivot_long
        
        if structural_sl_candidate > 0:
             sl_price = structural_sl_candidate - (0.5 * atr) 
             structural_sl_used = True
        else:
             sl_price = price - (ATR_TRAIL_MULTIPLIER * atr)
             
    else: 
        return 0.0, price - (ATR_TRAIL_MULTIPLIER * atr), False, 'N/A'


    bonus = 0.0
    fib_level = 'N/A'
    
    if side == 'ロング':
        distance = price - pivot_long
        
        if 0 < distance <= 2.5 * atr:
            bonus += 0.08 
            fib_level = "Support Zone"
            
        sma_long = df['sma'].iloc[-1] if 'sma' in df.columns and not df['sma'].isna().iloc[-1] else None
        if sma_long and price >= sma_long and price - sma_long < 3 * atr:
            bonus += 0.05
            fib_level += "/SMA50"

    return bonus, sl_price, structural_sl_used, fib_level


def analyze_single_timeframe(df_ohlcv: List[List[float]], timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    if not df_ohlcv or len(df_ohlcv) < REQUIRED_OHLCV_LIMITS.get(timeframe, 500):
        return None

    df = pd.DataFrame(df_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
    
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.adx(append=True)
    df.ta.stoch(append=True)
    df.ta.cci(append=True)
    df.ta.bbands(append=True) 
    df.ta.atr(length=14, append=True)
    df.ta.obv(append=True) 
    df['sma'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    df.dropna(inplace=True)
    if df.empty:
        return None

    latest = df.iloc[-1]
    price = latest['close']
    
    rsi = latest['RSI_14']
    macd_hist = latest['MACDh_12_26_9']
    adx = latest['ADX_14']
    stoch_k = latest['STOCHk_14_3_3']
    stoch_d = latest['STOCHd_14_3_3']
    cci = latest['CCI_14_0.015']
    atr = latest['ATR_14']
    volume = latest['volume']
    obv = latest['OBV']
    sma_long = latest['sma']
    
    score = BASE_SCORE 
    side = None
    tech_data = {}
    
    if rsi < RSI_MOMENTUM_LOW and cci < 0: 
        side = 'ロング'
        
        if rsi < RSI_OVERSOLD and stoch_k < 30: 
            score += 0.15 
            
        if macd_hist > 0 and latest['MACD_12_26_9'] > latest['MACDs_12_26_9']:
            score += 0.10 
            tech_data['macd_cross_valid'] = True
        else:
            tech_data['macd_cross_valid'] = False
            score -= MACD_CROSS_PENALTY 
            
        if volume > df['volume'].rolling(window=20).mean().iloc[-2] * VOLUME_CONFIRMATION_MULTIPLIER:
             score += 0.08 
             tech_data['volume_confirmation_bonus'] = 0.08
        else:
             tech_data['volume_confirmation_bonus'] = 0.0

    elif rsi > RSI_MOMENTUM_HIGH and cci > 0:
         side = 'ショート'
         

    if side == 'ロング':
        if timeframe == '4h' and sma_long and price < sma_long:
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_trend'] = "Short"
        else:
            tech_data['long_term_reversal_penalty'] = False
            tech_data['long_term_trend'] = "Long" if price >= sma_long else "Neutral"
            
        if stoch_k > 90 or stoch_d > 90:
            score -= 0.10
            tech_data['stoch_filter_penalty'] = 0.10
        else:
            tech_data['stoch_filter_penalty'] = 0.0
            
        bb_width = latest['BBL_5_2.0'] / latest['BBU_5_2.0'] if 'BBL_5_2.0' in df.columns else 1.0
        if abs(bb_width - 1.0) * 100 < VOLATILITY_BB_PENALTY_THRESHOLD:
            score -= 0.05 
            tech_data['volatility_bb_penalty'] = 0.05
        else:
            tech_data['volatility_bb_penalty'] = 0.0
            
        obv_sma = df['OBV'].rolling(window=20).mean().iloc[-2]
        if obv > obv_sma:
            score += OBV_MOMENTUM_BONUS
            tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        else:
            tech_data['obv_momentum_bonus_value'] = 0.0

    if side == 'ロング':
        struct_bonus, sl_price, structural_sl_used, fib_level = analyze_structural_proximity(df, price, side)
        score += struct_bonus
        
        risk_dist = price - sl_price
        if risk_dist <= 0: return None 
        
        tp1_price = price + (risk_dist * DTS_RRR_DISPLAY)
        
        ob_data = ORDER_BOOK_CACHE.get(symbol)
        if ob_data:
            if ob_data['bids_usdt'] > ob_data['asks_usdt'] * 1.5:
                score += LIQUIDITY_BONUS_POINT
                tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_POINT
            else:
                 tech_data['liquidity_bonus_value'] = 0.0
        else:
             tech_data['liquidity_bonus_value'] = 0.0
             
        fgi_proxy_bonus = macro_context.get('sentiment_fgi_proxy', 0.0)
        if fgi_proxy_bonus > 0:
             score += fgi_proxy_bonus
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy_bonus
        
        final_score = max(0.1, min(1.0, score))
        
        return {
            'timeframe': timeframe,
            'side': side,
            'price': price,
            'score': final_score,
            'entry': price, 
            'sl': sl_price,
            'tp1': tp1_price,
            'rr_ratio': DTS_RRR_DISPLAY, 
            'adx': adx,
            'tech_data': {
                 'rsi': rsi,
                 'macd_hist': macd_hist,
                 'adx': adx,
                 'stoch_k': stoch_k,
                 'cci': cci,
                 'atr': atr,
                 'structural_sl_used': structural_sl_used,
                 'fib_proximity_level': fib_level,
                 **tech_data
            }
        }
        
    return None


async def run_multi_timeframe_analysis(symbol: str, macro_context: Dict) -> List[Dict]:
    signals = []
    
    await fetch_order_book_depth(symbol)
    
    timeframes = ['15m', '1h', '4h']
    tasks = [fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf) for tf in timeframes]
    results = await asyncio.gather(*tasks)

    for i, (ohlcv, status, _) in enumerate(results):
        timeframe = timeframes[i]
        
        if status == "Success":
            signal = analyze_single_timeframe(ohlcv, timeframe, symbol, macro_context)
            if signal and signal['score'] >= SIGNAL_THRESHOLD:
                signals.append(signal)
                
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL / 3) 

    return signals
    
# ====================================================================================
# ACTUAL TRADING LOGIC (現物売買ロジック) (中略)
# ====================================================================================

async def get_open_spot_position() -> Tuple[float, Dict[str, Dict]]:
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    balance = await fetch_current_balance_usdt()
    
    open_orders: List = []
    try:
        symbols_to_check = list(ACTUAL_POSITIONS.keys())
        if symbols_to_check:
            for symbol in symbols_to_check:
                 orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol, params={'type': 'SPOT'})
                 open_orders.extend(orders)
                 await asyncio.sleep(0.5) 
             
    except Exception as e:
        logging.warning(f"オープンオーダーの取得エラー: {e}")
        return balance, ACTUAL_POSITIONS

    current_positions = ACTUAL_POSITIONS.copy()
    
    for symbol, pos in list(current_positions.items()):
        pos['open_orders'] = [o for o in open_orders if o['symbol'] == symbol]
        current_positions[symbol] = pos
        
        if not pos['open_orders']:
            if symbol in current_positions:
                 del current_positions[symbol]

    ACTUAL_POSITIONS = current_positions
    return balance, ACTUAL_POSITIONS


async def execute_spot_order(symbol: str, side: str, amount_coin: float, entry_price: float, sl_price: float, tp1_price: float) -> Optional[Dict]:
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    if side != 'ロング':
        logging.warning(f"{symbol}: 現物取引ではロング（買い）のみをサポートしています。")
        return None

    if amount_coin <= 0:
        logging.error(f"{symbol}: 取引量が0以下です。注文をスキップします。")
        return None

    order_info = None
    
    try:
        market_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount_coin 
        )
        
        filled_amount = float(market_order.get('filled', amount_coin))
        
        logging.info(f"✅ {symbol} | LONG: 成行買い注文成功。数量: {filled_amount:.4f}")

        sl_order = None
        tp_order = None
        
        if sl_price > 0:
            sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='limit', 
                side='sell',
                amount=filled_amount,
                price=sl_price,
                params={'timeInForce': 'GTC', 'type': 'SPOT'} 
            )
        
        if tp1_price > 0:
            tp_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='limit', 
                side='sell',
                amount=filled_amount,
                price=tp1_price,
                params={'timeInForce': 'GTC', 'type': 'SPOT'} 
            )

        ACTUAL_POSITIONS[symbol] = {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price, 
            'amount_coin': filled_amount,
            'amount_usdt': entry_price * filled_amount, 
            'sl': sl_price,
            'tp1': tp1_price,
            'open_orders': [o for o in [sl_order, tp_order] if o is not None],
            'entry_time': time.time()
        }
        
        order_info = ACTUAL_POSITIONS[symbol]
        await send_position_status_notification(f"🚀 新規エントリー: {symbol}", order_info)
        return order_info
        
    except Exception as e:
        logging.error(f"❌ {symbol} | 注文実行エラー: {type(e).__name__} - {e}")
        return None


async def close_spot_position(symbol: str, close_type: str = 'market') -> bool:
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    if symbol not in ACTUAL_POSITIONS:
        logging.warning(f"{symbol}: ポジションが存在しません。")
        return False
        
    pos = ACTUAL_POSITIONS[symbol]

    try:
        cancel_tasks = []
        for order in pos['open_orders']:
            try:
                cancel_tasks.append(EXCHANGE_CLIENT.cancel_order(order['id'], symbol=symbol, params={'type': 'SPOT'})) 
            except Exception as e:
                logging.warning(f"{symbol}: 注文ID {order['id']} のキャンセルエラー: {e}")
                
        await asyncio.gather(*cancel_tasks, return_exceptions=True)
        
        logging.info(f"✅ {symbol}: SL/TP注文をすべてキャンセルしました。")
        
        amount_to_sell = pos['amount_coin']
        
        if close_type == 'market':
            close_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount_to_sell,
                params={'type': 'SPOT'} 
            )
            logging.info(f"✅ {symbol}: 成行決済注文成功。約定量: {close_order.get('filled', 0):.4f}")
        else:
             logging.warning(f"{symbol}: サポートされていない決済タイプ ({close_type}) です。")
             return False

        del ACTUAL_POSITIONS[symbol]
        await send_position_status_notification(f"🛑 ポジション決済完了: {symbol} ({close_type})")
        return True
        
    except Exception as e:
        logging.error(f"❌ {symbol} | 決済エラー: {type(e).__name__} - {e}")
        return False


async def check_and_handle_spot_orders():
    global ACTUAL_POSITIONS
    
    _, positions = await get_open_spot_position()
    
    symbols_to_delete = []
    
    for symbol, pos in positions.items():
        tp_open = any(o['price'] == pos['tp1'] and o['side'] == 'sell' for o in pos['open_orders'])
        sl_open = any(o['price'] == pos['sl'] and o['side'] == 'sell' for o in pos['open_orders'])
        
        if not tp_open and not sl_open:
            symbols_to_delete.append(symbol)
            logging.info(f"✅ {symbol}: TP/SLのオープンオーダーが確認できませんでした。ポジションをクローズ済みと見なします。")

    for symbol in symbols_to_delete:
         if symbol in ACTUAL_POSITIONS:
             del ACTUAL_POSITIONS[symbol]
             await send_position_status_notification(f"✅ ポジション終了 (TP/SL約定): {symbol}")


async def notify_signals_in_queue():
    global LAST_ANALYSIS_SIGNALS, TRADE_NOTIFIED_SYMBOLS
    
    now = time.time()
    TRADE_NOTIFIED_SYMBOLS = {s: t for s, t in TRADE_NOTIFIED_SYMBOLS.items() if now - t < TRADE_SIGNAL_COOLDOWN}
    
    high_conviction_signals = [
        sig for sig in LAST_ANALYSIS_SIGNALS
        if sig['score'] >= SIGNAL_THRESHOLD and sig['side'] == 'ロング' and sig['symbol'] not in TRADE_NOTIFIED_SYMBOLS
    ]
    
    high_conviction_signals.sort(key=lambda x: (x['score'], x['rr_ratio']), reverse=True)
    
    if not high_conviction_signals:
        return
        
    signals_to_act = high_conviction_signals[:TOP_SIGNAL_COUNT]
    
    balance_usdt = await fetch_current_balance_usdt()
    
    for rank, best_signal in enumerate(signals_to_act):
        symbol = best_signal['symbol']
        message = format_integrated_analysis_message(symbol, [best_signal], rank + 1)
        
        if symbol in ACTUAL_POSITIONS:
            send_telegram_html(f"💡 {symbol}: シグナル検出しましたが、既にポジションがあります。\n\n{message}")
            TRADE_NOTIFIED_SYMBOLS[symbol] = now
            continue
            
        entry_price = best_signal['entry']
        pos_usdt_value, amount_coin = calculate_position_size(entry_price, balance_usdt)
        
        if amount_coin == 0 or pos_usdt_value < 10: 
            send_telegram_html(f"⚠️ {symbol} | 残高不足または最低取引額以下のため注文できません。\n残高: ${format_usdt(balance_usdt)}\n\n{message}")
            TRADE_NOTIFIED_SYMBOLS[symbol] = now
            continue

        order_result = await execute_spot_order(
            symbol=symbol,
            side=best_signal['side'],
            amount_coin=amount_coin,
            entry_price=entry_price,
            sl_price=best_signal['sl'],
            tp1_price=best_signal['tp1']
        )
        
        if order_result:
            TRADE_NOTIFIED_SYMBOLS[symbol] = now
        else:
            send_telegram_html(f"❌ {symbol} | 注文失敗（APIエラー等）によりエントリーできませんでした。\n\n{message}")
            TRADE_NOTIFIED_SYMBOLS[symbol] = now


async def main_loop():
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME
    
    while True:
        try:
            now = time.time()
            
            if EXCHANGE_CLIENT:
                await check_and_handle_spot_orders()

            if now - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(5)
                continue

            LAST_UPDATE_TIME = now
            
            await update_symbols_by_volume()
            
            crypto_macro = await get_crypto_macro_context()
            GLOBAL_MACRO_CONTEXT.update(crypto_macro)
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, クライアント: {CCXT_CLIENT_NAME} Spot)")

            analysis_tasks = [
                run_multi_timeframe_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            all_results: List[List[Dict]] = await asyncio.gather(*analysis_tasks)
            
            LAST_ANALYSIS_SIGNALS = [signal for signals in all_results for signal in signals]
            
            await notify_signals_in_queue()
            
            if now - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_NOTIFICATION_INTERVAL:
                 await send_position_status_notification("🕐 1時間 定期ステータス通知")
                 LAST_HOURLY_NOTIFICATION_TIME = now
            
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
                if EXCHANGE_CLIENT:
                    await EXCHANGE_CLIENT.close()
            except:
                pass 
            
            logging.error(f"メインループで致命的なエラー: {error_name} - {e}")
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.3 - Balance Re-Hotfix") 

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.3 Startup initializing (Balance Re-Hotfix)...") 
    
    await initialize_ccxt_client()
    
    await send_position_status_notification("🤖 初回起動通知")
    
    global LAST_HOURLY_NOTIFICATION_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time() 
    
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status(request: Request): 
    client_ip = request.client.host if request.client else "Unknown"
    
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.3 - Balance Re-Hotfix", 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions": len(ACTUAL_POSITIONS),
        "client_ip_requesting": client_ip 
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view(request: Request): 
    client_ip = request.client.host if request.client else "Unknown"
    logging.info(f"API Access - IP: {client_ip}") 
    
    return JSONResponse(content={
        "message": f"Apex BOT API is running. Version: v19.0.3 - Balance Re-Hotfix",
        "client_ip": client_ip 
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
