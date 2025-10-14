# ====================================================================================
# Apex BOT v19.0.0 - MEXC Spot Trading Implementation
# 
# 修正ポイント:
# 1. 【取引所変更】CCXTクライアントをOKXからMEXC (mexc) に変更。
# 2. 【現物取引】defaultType='spot'に設定。
# 3. 【実際の売買】SIMULATED_POSITIONSを廃止し、CCXTのcreate_order, fetch_balance, fetch_open_ordersを使用。
# 4. 【注文ロジック】新規ロング注文（成行買い）と、SL/TP指値注文（全数量売り）を同時に発注。
# 5. 【決済ロジック】SL/TP到達を市場価格でチェックし、指値キャンセルと成行全決済を実行。
# 6. 【Shortシグナル】現物取引の新規Shortは複雑なため、シグナルをNeutralとして処理。
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

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
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

# 環境変数から取得。
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.75             
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

# 💡 削除: Funding Rate Bias Filter Parameters

# 💡 削除: Dominance Bias Filter Parameters

# 💡【ファンダメンタルズ】流動性/スマートマネーフィルター Parameters
LIQUIDITY_BONUS_POINT = 0.06        
ORDER_BOOK_DEPTH_LEVELS = 5         
OBV_MOMENTUM_BONUS = 0.04           
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
CCXT_CLIENT_NAME: str = 'MEXC' # 💡 変更: MEXC 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT] # Spotシンボル形式
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} # オーダーブックキャッシュ

# 💡 変更: 実際のポジション追跡用 (CCXTから取得した残高ベースで管理)
ACTUAL_POSITIONS: Dict[str, Dict] = {} # {symbol: {side, entry_price, amount_coin, amount_usdt, sl, tp1, open_orders: [...]}}

LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
HOURLY_NOTIFICATION_INTERVAL = 60 * 60 # 1時間

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

# ... (get_tp_reach_time, format_price_utility, format_usdt, format_pnl) ...

def calculate_pnl_utility(side: str, entry_price: float, current_price: float, amount_usdt: float, amount_coin: float) -> Tuple[float, float]:
    """現在のP&LとP&L%を計算"""
    if amount_coin == 0 or entry_price == 0:
        return 0.0, 0.0
        
    if side == "ロング":
        pnl = amount_coin * (current_price - entry_price)
    else: # 現物取引の新規ショートは扱わないため、ここでは0を返す
        return 0.0, 0.0
        
    # amount_usdt (ポジションサイズ) を分母に使用
    pnl_percent = (pnl / amount_usdt) * 100 if amount_usdt > 0 else 0.0
    return pnl, pnl_percent
    
def calculate_position_size(price: float, balance_usdt: float) -> Tuple[float, float]:
    """固定リスクに基づき現物ポジションサイズ (USDT値とコイン数量) を計算する"""
    # 💡 修正: 残高の5%か$500の小さい方を取引額とする
    POSITION_USDT_VALUE = min(balance_usdt * 0.05, 500.0) 
    if POSITION_USDT_VALUE < 10: # 最低取引額
         return 0.0, 0.0
         
    # 概算のコイン数量。CCXTのcreate_orderで丸められることを期待
    amount_coin = POSITION_USDT_VALUE / price
    return POSITION_USDT_VALUE, amount_coin


# ... (send_telegram_html, get_estimated_win_rate) ...

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    【v19.0.0】現物取引用に調整した通知メッセージを生成する
    """
    
    valid_signals = [s for s in signals if s.get('side') == 'ロング'] # 💡 ロングシグナルのみを対象
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
    tp1_price = best_signal.get('tp1', 0.0) 
    entry_type = "Market/Limit" # Spot取引では成行または指値
    
    display_symbol = symbol
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
    direction_emoji = "🚀"
    direction_text = "<b>現物買い (LONG)</b>"
        
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
        f"<b>{display_symbol}</b> | {direction_emoji} {direction_text} (MEXC Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # --- 取引計画部 (SL/TPは指値注文として発注) ---
    sl_width = abs(entry_price - sl_price)
    sl_source_str = "ATR基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 ATR バッファ"
        
    trade_plan = (
        f"<b>✅ 取引計画 (現物買い/指値売り自動設定)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>エントリー種別</b>: <code>{entry_type}</code> (成行で買い)\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>自動損切り (SL 指値)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>自動利確 (TP 指値)</b>: <code>${format_price_utility(tp1_price, symbol)}</code> (動的追跡開始点)\n"
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
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n\n" 
    )

    # --- 分析の根拠部 ---
    long_term_trend_ok = not tech_data.get('long_term_reversal_penalty', False)
    momentum_ok = tech_data.get('macd_cross_valid', True) and not tech_data.get('stoch_filter_penalty', 0) > 0
    structure_ok = tech_data.get('structural_pivot_bonus', 0.0) > 0
    volume_confirm_ok = tech_data.get('volume_confirmation_bonus', 0.0) > 0
    obv_confirm_ok = tech_data.get('obv_momentum_bonus_value', 0.0) > 0
    liquidity_ok = tech_data.get('liquidity_bonus_value', 0.0) > 0
    # 💡 削除: funding_rate_ok (現物取引のため)
    # 💡 削除: dominance_ok (現物取引のため)
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
        # 💡 削除: Funding Rate, Dominance Bias
    )
    
    # --- フッター部 ---
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ 成行買いを実行し、SL/TP指値売り注文を自動設定します。</pre>"
        f"<i>Bot Ver: v19.0.0 (MEXC Spot Trading)</i>"
    )

    return header + trade_plan + summary + analysis_details + footer


async def send_position_status_notification(event_type: str, new_order_info: Optional[Dict] = None):
    """
    ポジション情報、残高、P&Lなどを整形し、Telegramに通知する
    """
    global ACTUAL_POSITIONS, EXCHANGE_CLIENT
    
    # 1. 残高とポジションの更新
    current_balance, positions = await get_open_spot_position()
    ACTUAL_POSITIONS = positions # グローバル変数を最新の状態に更新
    
    # 2. 現在のポジション情報の準備
    if not ACTUAL_POSITIONS:
        # ポジションがない場合の通知
        message = (
            f"<b>📊 {event_type} - アカウントステータス</b>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - <b>現在の残高 (USDT)</b>: <code>${format_usdt(current_balance)}</code>\n"
            f"  - <b>オープンポジション</b>: <code>なし</code>\n"
            f"  - <b>取引所</b>: <code>{CCXT_CLIENT_NAME} Spot</code>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: v19.0.0 (MEXC Spot Trading)</i>"
        )
        send_telegram_html(message)
        return

    # 3. ポジションがある場合の詳細情報の構築
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
        
        # 最新価格の取得
        latest_price = pos['entry_price'] 
        try:
             ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
             latest_price = ticker.get('last', pos['entry_price'])
        except Exception:
             pass 
             
        # 現在の損益を計算 (P&Lはエントリー価格と最新価格から計算)
        current_pnl, pnl_percent = calculate_pnl_utility(pos['side'], pos['entry_price'], latest_price, pos['amount_usdt'], pos['amount_coin'])
        total_unrealized_pnl += current_pnl
        
        # TP/SL到達時の損益を計算
        pnl_at_tp = calculate_pnl_utility('ロング', pos['entry_price'], pos['tp1'], pos['amount_usdt'], pos['amount_coin'])[0] if pos['tp1'] > 0 else 0.0
        pnl_at_sl = calculate_pnl_utility('ロング', pos['entry_price'], pos['sl'], pos['amount_usdt'], pos['amount_coin'])[0] if pos['sl'] > 0 else 0.0
        
        # SL/TP注文のステータスチェック
        sl_order_open = any(o['price'] == pos['sl'] and o['side'] == 'sell' for o in pos['open_orders'])
        tp_order_open = any(o['price'] == pos['tp1'] and o['side'] == 'sell' for o in pos['open_orders'])
        
        # メッセージ整形
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

    # 総P&Lを追加
    message += (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>合計未実現損益</b>: {format_pnl(total_unrealized_pnl)}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.0 (MEXC Spot Trading)</i>"
    )
    
    send_telegram_html(message)


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (MEXC)"""
    global EXCHANGE_CLIENT
    
    # 💡 修正: MEXCのAPIキーを環境変数から読み込む
    mexc_key = os.environ.get('MEXC_API_KEY')
    mexc_secret = os.environ.get('MEXC_SECRET')
    
    config = {
        'timeout': 30000, 
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}, # 💡 修正: Spot取引に設定
        'apiKey': mexc_key,
        'secret': mexc_secret,
    }
    
    EXCHANGE_CLIENT = ccxt_async.mexc(config) # 💡 修正: mexcクライアント
    
    if EXCHANGE_CLIENT:
        auth_status = "認証済み" if mexc_key and mexc_secret else "公開データのみ"
        logging.info(f"CCXTクライアントを初期化しました ({CCXT_CLIENT_NAME} - {auth_status}, Default: Spot)")
    else:
        logging.error("CCXTクライアントの初期化に失敗しました。")

async def fetch_current_balance_usdt() -> float:
    """CCXTから現在のUSDT残高を取得する。失敗した場合は0を返す。"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        # SpotアカウントのUSDT残高を取得
        usdt_free = balance['USDT']['free']
        return usdt_free
    except Exception as e:
        # APIキーがない/エラーの場合は残高0として処理
        logging.error(f"残高取得エラー（APIキー未設定/エラーの可能性）: {e}")
        return 0.0

# 💡 削除: convert_symbol_to_okx_swap (Spotでは不要)
# 💡 削除: fetch_funding_rate (Spotでは不要)


async def update_symbols_by_volume():
    """CCXTを使用してMEXCの出来高トップ30のUSDTペア銘柄を動的に取得・更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        # Spot取引所としてtickersを取得
        await EXCHANGE_CLIENT.load_markets() 
        
        usdt_tickers = {}
        # marketsからUSDTペアのSpotをフィルタリング
        for symbol, market in EXCHANGE_CLIENT.markets.items():
            if market['active'] and market['quote'] == 'USDT' and market['spot']:
                try:
                    # 出来高ベースでフィルタリングするため、tickerを取得
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
        
        # シンボル形式はCCXTで使われる'BTC/USDT'形式
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
        # SpotとしてOHLCVを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < 30: 
            return [], "DataShortage", client_name
            
        return ohlcv, "Success", client_name

    except Exception as e:
        return [], "ExchangeError", client_name


async def get_crypto_macro_context() -> Dict:
    """
    💡 マクロ市場コンテキストを取得 (FGI Proxy, BTC/ETH Trend)
    """
    
    # 1. BTC/USDTとETH/USDTの長期トレンドと直近の価格変化率を取得 (4h足)
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC/USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH/USDT", '4h')
    
    btc_trend = 0
    eth_trend = 0
    # ... (DataFrame & SMA calculation logic is the same) ...
    df_btc = pd.DataFrame()
    df_eth = pd.DataFrame()

    if status_btc == "Success":
        df_btc = pd.DataFrame(btc_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        df_btc['sma'] = ta.sma(df_btc['close'], length=LONG_TERM_SMA_LENGTH) 
        df_btc.dropna(subset=['sma'], inplace=True)
        if not df_btc.empty:
            if df_btc['close'].iloc[-1] > df_btc['sma'].iloc[-1]: btc_trend = 1 
            elif df_btc['close'].iloc[-1] < df_btc['sma'].iloc[-1]: btc_trend = -1 
    
    if status_eth == "Success":
        df_eth = pd.DataFrame(eth_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        df_eth['sma'] = ta.sma(df_eth['close'], length=LONG_TERM_SMA_LENGTH)
        df_eth.dropna(subset=['sma'], inplace=True)
        if not df_eth.empty:
            if df_eth['close'].iloc[-1] > df_eth['sma'].iloc[-1]: eth_trend = 1 
            elif df_eth['close'].iloc[-1] < df_eth['sma'].iloc[-1]: eth_trend = -1 

    # 2. 💡【恐怖指数】FGI Proxyの計算
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        sentiment_score = FGI_PROXY_BONUS_MAX 
    elif btc_trend == -1 and eth_trend == -1:
        sentiment_score = -FGI_PROXY_BONUS_MAX 
        
    # 3. 💡 削除: BTC Dominance Proxyの計算
    
    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        'fx_bias': 0.0 # FXバイアスはメインループで更新されるが、初期値として
    }


# ====================================================================================
# CORE ANALYSIS LOGIC
# ====================================================================================

# ... (calculate_fib_pivot, analyze_structural_proximity - 変更なし) ...

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """ 単一の時間軸で分析とシグナル生成を行う関数 (v19.0.0) """
    
    # 1. データ取得とOrder Book取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    order_book_data = None
    
    if timeframe == '1h':
        # 💡 削除: funding_rate_val = await fetch_funding_rate(symbol)
        order_book_data = await fetch_order_book_depth(symbol)

    if status != "Success":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': status, 'score': 0.00, 'rrr_net': 0.00}

    # 2. DataFrameの準備 (変更なし)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df.set_index(pd.to_datetime(df['timestamp'], unit='ms'), inplace=True)
    
    if df.empty or len(df) < 40:
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'DataShortage', 'score': 0.00, 'rrr_net': 0.00}
    
    # 3. テクニカル指標の計算 (変更なし)
    # ... (テクニカル指標計算ロジック) ...
    # ... (MACDクロス/BBands/Pivot計算ロジック) ...
    
    last_row = df.iloc[-1]
    current_price = last_row['close']
    current_atr = last_row['ATR']
    
    # 4. シグナルスコアの計算
    
    # ... (スコアリング変数初期化) ...
    structural_pivot_bonus = 0.0
    liquidity_bonus_value = 0.0
    # 💡 削除: funding_rate_bonus_value = 0.0
    # 💡 削除: dominance_bias_bonus_value = 0.0
    obv_momentum_bonus_value = 0.0
    sentiment_fgi_proxy_bonus = macro_context.get('sentiment_fgi_proxy', 0.0) 
    long_term_reversal_penalty_value = 0.0
    stoch_filter_penalty = 0.0 
    
    # ... (Trend/Momentum checks - 変更なし) ...
    
    # --- シグナル候補の決定 ---
    
    # 1. MACDゴールデンクロス、またはRSIの過売ゾーンからの反転 (ロングシグナル)
    if last_row['MACD_CROSS_UP'] or (last_row['RSI'] >= RSI_MOMENTUM_LOW and df['RSI'].iloc[-2] < RSI_MOMENTUM_LOW):
        side = "ロング"
        score += volume_confirmation_bonus
        if last_row['MACD_CROSS_UP']: score += 0.05
        if last_row['RSI'] < RSI_OVERSOLD: score += 0.05 
            
        # OBVトレンド確認: OBVがSMAを上回っているか
        if last_row['OBV'] > last_row['OBV_SMA']:
            score += OBV_MOMENTUM_BONUS
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
            
        # MACDデッドクロスによるペナルティ
        if last_row['MACD_CROSS_DOWN']:
             score -= MACD_CROSS_PENALTY
             macd_cross_valid = False

    # 2. MACDデッドクロス、またはRSIの過買ゾーンからの反転 (ショートシグナル -> 現物では新規取引しない)
    elif last_row['MACD_CROSS_DOWN'] or (last_row['RSI'] <= RSI_MOMENTUM_HIGH and df['RSI'].iloc[-2] > RSI_MOMENTUM_HIGH):
        # 💡 修正: 現物取引では新規ショートは不可/非推奨のため、Neutralとする
        side = "Neutral"
        macd_cross_valid = False

    # シグナルがない場合は終了
    if side == "Neutral":
        return {'symbol': symbol, 'timeframe': timeframe, 'side': 'Neutral', 'score': 0.00, 'rrr_net': 0.00}
        
    # --- 共通のボーナス/ペナルティ適用 ---

    # ... (構造的ボーナス/Volatility Filter - 変更なし) ...

    # 💡 削除: Funding Rate Filter (Spot取引のため)

    # 流動性/オーダーブックの確認 (1h足のみ)
    if timeframe == '1h' and order_book_data:
        ratio = order_book_data.get('ask_bid_ratio', 1.0)
        
        if side == "ロング":
            if ratio < 1.0: # 買い板が厚い (Ask/Bid < 1.0)
                score += LIQUIDITY_BONUS_POINT
                liquidity_bonus_value = LIQUIDITY_BONUS_POINT
        # 💡 Shortのロジックは削除
                
    # FGI Proxy/市場センチメントの適用
    if (side == "ロング" and sentiment_fgi_proxy_bonus > 0): # 💡 ロングシグナルのみ
         score += abs(sentiment_fgi_proxy_bonus)
    elif sentiment_fgi_proxy_bonus != 0:
         score -= abs(sentiment_fgi_proxy_bonus) 

    # 💡 削除: BTC Dominance Bias (Spot取引のため)
        
    # 極端な過熱感ペナルティを適用
    score -= stoch_filter_penalty
    
    # 5. エントリー・SL・TPの計算 (変更なし)
    # ... (SL/TP calculation logic) ...
    # ... (RRR calculation logic) ...
    
    final_score = max(0.01, min(0.99, score))
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'rr_ratio': rr_ratio, 
        'rrr_net': final_score * rr_ratio, 
        'price': current_price,
        'entry': current_price, 
        'sl': sl_price,
        'tp1': tp1_price, 
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
            'funding_rate_bonus_value': 0.0, # Spotのため0
            'dominance_bias_bonus_value': 0.0, # Spotのため0
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
    # ... (logic is the same) ...
    # ... (Long term trend logic) ...
    
    timeframes = ['15m', '1h', '4h']
    tasks = []
    
    for tf in timeframes:
        tasks.append(
            analyze_single_timeframe(
                symbol, tf, macro_context, CCXT_CLIENT_NAME, long_term_trend, long_term_penalty_applied
            )
        )
        await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL) 
        
    results = await asyncio.gather(*tasks)
    
    signals: List[Dict] = []
    for result in results:
        # 💡 修正: Longシグナルのみを返す
        if result and result.get('side') == 'ロング':
            signals.append(result)

    return signals


# ====================================================================================
# ACTUAL TRADING LOGIC (現物売買ロジック)
# ====================================================================================

async def get_open_spot_position() -> Tuple[float, Dict[str, Dict]]:
    """
    保有している現物ポジションとオープンオーダーを取得・整形する
    :return: (USDT残高, {symbol: {side, entry_price, amount_coin, amount_usdt, sl, tp1, open_orders: [...]}})
    """
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    usdt_balance = await fetch_current_balance_usdt()
    
    if not EXCHANGE_CLIENT:
        return usdt_balance, {}
        
    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        open_orders = await EXCHANGE_CLIENT.fetch_open_orders()
        
        current_positions = {}
        
        # 1. USDT以外の現物残高をポジションとして検出
        for asset, data in balance['total'].items():
            if asset == 'USDT' or data <= 0.0 or balance[asset]['free'] <= 0.0: # free残高もチェック
                continue
                
            symbol = f"{asset}/USDT"
            
            # 前回ACTUAL_POSITIONSに存在すれば、そのentry_price/SL/TPを流用
            entry_price = ACTUAL_POSITIONS.get(symbol, {}).get('entry_price', 0.0) 
            sl_price_prev = ACTUAL_POSITIONS.get(symbol, {}).get('sl', 0.0)
            tp1_price_prev = ACTUAL_POSITIONS.get(symbol, {}).get('tp1', 0.0)
            
            amount_coin = balance[asset]['total'] # 総量
            
            # 最新価格の取得
            latest_price = 0.0
            try:
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                latest_price = ticker.get('last', 0.0)
            except Exception:
                pass 
                
            # エントリー価格が不明な場合、最新価格で概算 (P&Lは不正確になる)
            if entry_price == 0.0 and latest_price > 0.0:
                 entry_price = latest_price
                 
            amount_usdt = amount_coin * latest_price
            
            # 2. 該当シンボルのオープンオーダー（SL/TP指値注文）を収集
            symbol_open_orders = [o for o in open_orders if o['symbol'] == symbol and o['side'] == 'sell']
            
            # 3. ポジション情報を構築
            current_positions[symbol] = {
                'side': 'ロング', 
                'entry_price': entry_price, 
                'amount_coin': amount_coin,
                'amount_usdt': amount_usdt,
                'sl': sl_price_prev, 
                'tp1': tp1_price_prev,
                'open_orders': symbol_open_orders
            }
            
        return usdt_balance, current_positions

    except Exception as e:
        logging.error(f"現物ポジション/オーダー取得エラー: {e}")
        return usdt_balance, {}


async def execute_spot_order(signal: Dict) -> Optional[Dict]:
    """
    現物取引の注文を実行する (新規成行注文 + SL/TP指値注文)
    """
    global EXCHANGE_CLIENT
    symbol = signal['symbol']
    side = signal['side']
    entry_price_sig = signal['entry'] # シグナルの価格
    sl_price = signal['sl']
    tp1_price = signal['tp1']

    if EXCHANGE_CLIENT is None:
        logging.error("CCXTクライアントが初期化されていません。注文をスキップします。")
        return None
        
    if side != 'ロング':
        return None
        
    try:
        # 1. 注文サイズの計算
        usdt_balance = await fetch_current_balance_usdt()
        amount_usdt, amount_coin_raw = calculate_position_size(entry_price_sig, usdt_balance)
        
        if amount_usdt == 0.0:
            logging.warning(f"{symbol} のポジションサイズが最低基準 ($10) 未満のため、注文をスキップします。")
            return None
            
        # 2. 新規成行買い注文 (Market Buy)
        market = EXCHANGE_CLIENT.market(symbol)
        amount_coin = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_coin_raw)
        
        logging.info(f"➡️ {symbol} {side} 成行注文を実行 (数量: {amount_coin} 予想コスト: {amount_usdt:.2f} USDT)...")
        
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount_coin,
            params={}
        )
        
        # 実際の約定価格を取得 (ここでは成行のため、最新価格またはシグナル価格を使用)
        entry_price_actual = entry_price_sig 
        try:
             ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
             entry_price_actual = ticker.get('last', entry_price_sig)
        except Exception:
             pass 
             
        logging.info(f"✅ {symbol} 成行注文成功: ID {order['id']}")

        # 3. SL/TP指値注文 (Sell Limit) を発注 (数量は成行注文で約定した数量を使用するのが理想だが、ここでは発注した数量を使用)
        
        # SL指値
        sl_amount = amount_coin
        sl_price_precise = EXCHANGE_CLIENT.price_to_precision(symbol, sl_price)
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=sl_amount,
            price=sl_price_precise,
            params={}
        )
        logging.info(f"✅ {symbol} SL指値注文成功: ID {sl_order['id']} @ {sl_price_precise}")
        
        # TP指値
        tp_amount = amount_coin
        tp_price_precise = EXCHANGE_CLIENT.price_to_precision(symbol, tp1_price)
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=tp_amount,
            price=tp_price_precise,
            params={}
        )
        logging.info(f"✅ {symbol} TP指値注文成功: ID {tp_order['id']} @ {tp_price_precise}")
        
        return {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price_actual, 
            'amount_coin': float(amount_coin),
            'amount_usdt': amount_usdt,
            'sl': sl_price,
            'tp1': tp1_price,
            'main_order_id': order['id'],
            'sl_order_id': sl_order['id'],
            'tp_order_id': tp_order['id'],
        }

    except Exception as e:
        logging.error(f"現物注文実行エラー ({symbol}): {e}")
        send_telegram_html(f"<b>❌ 注文エラー: {symbol} ロング</b>\n<pre>{e}</pre>")
        return None


async def close_spot_position(symbol: str, position_info: Dict, closed_by: str) -> Optional[Dict]:
    """
    現物ポジションを全決済し、残っているSL/TP指値注文をキャンセルする
    """
    global EXCHANGE_CLIENT
    
    if EXCHANGE_CLIENT is None:
        return None
        
    try:
        # 1. 残っているオープンオーダーを全てキャンセル
        open_orders = position_info.get('open_orders', [])
        cancel_tasks = []
        for order in open_orders:
            cancel_tasks.append(EXCHANGE_CLIENT.cancel_order(order['id'], symbol))
            
        await asyncio.gather(*cancel_tasks, return_exceptions=True)
        logging.info(f"✅ {symbol} のオープンオーダー ({len(open_orders)} 件) をキャンセルしました。")
        
        # 2. ポジションを成行で全決済 (Market Sell)
        balance = await EXCHANGE_CLIENT.fetch_balance()
        base_asset = symbol.split('/')[0]
        amount_coin = balance.get(base_asset, {}).get('free', 0.0) # Free残高を全て売却
        
        if amount_coin > 0.0:
            amount_coin_precise = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_coin)
            logging.info(f"➡️ {symbol} 成行決済注文を実行 (数量: {amount_coin_precise} コイン)...")
            
            close_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=amount_coin_precise
            )
            logging.info(f"✅ {symbol} 成行決済注文成功: ID {close_order['id']}")
            
            return {
                'symbol': symbol,
                'closed_by': closed_by,
                'close_order_id': close_order['id']
            }
        
        else:
             logging.warning(f"⚠️ {symbol} 決済時、保有数量が0のためスキップしました。")
             return {'symbol': symbol, 'closed_by': closed_by, 'close_order_id': 'N/A'}


    except Exception as e:
        logging.error(f"現物決済実行エラー ({symbol}): {e}")
        send_telegram_html(f"<b>❌ 決済エラー: {symbol}</b>\n<pre>{e}</pre>")
        return None


async def check_and_handle_spot_orders():
    """オープンポジションのSL/TP到達をチェックし、利確/損切処理を行う"""
    global ACTUAL_POSITIONS, EXCHANGE_CLIENT
    
    # 1. 最新のポジションとオープンオーダーを取得
    usdt_balance, positions = await get_open_spot_position()
    
    # 2. 決済が必要なポジションをチェック
    positions_to_close = []
    
    # 最新価格の取得タスクを作成
    ticker_tasks = [EXCHANGE_CLIENT.fetch_ticker(symbol) for symbol in positions.keys()]
    ticker_results = await asyncio.gather(*ticker_tasks, return_exceptions=True)
    latest_prices = {}
    for i, symbol in enumerate(positions.keys()):
        if not isinstance(ticker_results[i], Exception):
            latest_prices[symbol] = ticker_results[i].get('last', 0.0)

    for symbol, pos in list(positions.items()):
        latest_price = latest_prices.get(symbol, 0.0)
        if latest_price == 0.0:
            continue
            
        # SL/TP指値がオープンかどうか
        sl_order_open = any(o['price'] == pos['sl'] for o in pos['open_orders'])
        tp_order_open = any(o['price'] == pos['tp1'] for o in pos['open_orders'])
        
        closed_by = None
        
        # A. 市場価格がTP/SLを突き抜けた場合の緊急決済 (成行)
        # 利確判定 (TP1を上回っている AND TP指値がオープンな場合)
        if latest_price >= pos['tp1'] and tp_order_open:
            closed_by = "利確 (Market Price Reached TP)"
        
        # 損切判定 (SLを下回っている AND SL指値がオープンな場合)
        elif latest_price <= pos['sl'] and sl_order_open:
            closed_by = "損切 (Market Price Hit SL)"
            
        if closed_by:
            # 緊急決済を実行
            close_result = await close_spot_position(symbol, pos, closed_by)
            if close_result:
                positions_to_close.append((symbol, pos, closed_by, latest_price))
            
    # 3. 決済通知
    for symbol, pos, close_reason, close_price in positions_to_close:
        # P&Lを再計算 (決済価格=close_price)
        pnl, _ = calculate_pnl_utility(pos['side'], pos['entry_price'], close_price, pos['amount_usdt'], pos['amount_coin'])
        
        # 決済通知を作成 
        await send_telegram_html(
            f"<b>🚨 ポジション決済 ({close_reason}) - {symbol}</b>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - <b>銘柄</b>: <code>{symbol}</code> (ロング)\n"
            f"  - <b>決済価格</b>: <code>${format_price_utility(close_price, symbol)}</code>\n"
            f"  - <b>実現損益 (概算)</b>: {format_pnl(pnl)}\n" 
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: v19.0.0 (MEXC Spot Trading)</i>"
        )
    
    # 4. ACTUAL_POSITIONSを最新のCCXT残高に同期
    # get_open_spot_positionが既に呼ばれているため、ACTUAL_POSITIONSは最新の状態に更新される。
    # ここではACTUAL_POSITIONSを明示的に更新する必要はない。


async def notify_signals_in_queue():
    """通知キューにあるシグナルをチェックし、クールダウンを考慮して通知する & 注文を実行する"""
    global LAST_ANALYSIS_SIGNALS, TRADE_NOTIFIED_SYMBOLS, ACTUAL_POSITIONS
    
    if not LAST_ANALYSIS_SIGNALS:
        return

    # 総合優位性スコア (P-Score * RRR) でソートし、閾値以上のロングシグナルを抽出
    high_value_signals = sorted(
        [s for s in LAST_ANALYSIS_SIGNALS if s.get('rrr_net', 0.0) >= (SIGNAL_THRESHOLD * 0.8)],
        key=lambda x: x.get('rrr_net', 0.0), 
        reverse=True
    )

    notified_count = 0
    now = time.time()
    
    # ... (Logging logic) ...

    for rank, signal in enumerate(high_value_signals, 1):
        symbol = signal['symbol']
        
        # クールダウンチェック
        if symbol in TRADE_NOTIFIED_SYMBOLS and (now - TRADE_NOTIFIED_SYMBOLS[symbol]) < TRADE_SIGNAL_COOLDOWN:
            # ... (Cool down logging) ...
            continue
            
        # 注文実行前に、既にポジションがないか確認
        if symbol in ACTUAL_POSITIONS:
            logging.info(f"⚠️ {symbol} は既にポジションを保有しています。新規注文をスキップします。")
            continue
            
        # 1. 注文を実行
        order_info = await execute_spot_order(signal)
        
        if order_info:
            # 2. 注文成功時: ポジション情報をACTUAL_POSITIONSに格納 (get_open_spot_positionに備え、SL/TP情報を保持)
            ACTUAL_POSITIONS[symbol] = {
                'side': order_info['side'],
                'entry_price': order_info['entry_price'],
                'sl': order_info['sl'],
                'tp1': order_info['tp1'],
                'amount_usdt': order_info['amount_usdt'], 
                'amount_coin': order_info['amount_coin'],
                'open_orders': [] # 注文完了直後は空だが、次のループでfetchされる
            }
            
            # 3. Telegram通知（シグナル通知とポジションステータス）
            message = format_integrated_analysis_message(symbol, [s for s in LAST_ANALYSIS_SIGNALS if s['symbol'] == symbol], rank)
            send_telegram_html(message) 
            await send_position_status_notification(f"✅ ポジション取得 (MEXC SPOT: {symbol})", order_info)
            
            TRADE_NOTIFIED_SYMBOLS[symbol] = now
            notified_count += 1
            if notified_count >= TOP_SIGNAL_COUNT:
                break
        
        await asyncio.sleep(1.0) # Telegram APIレートリミット回避


async def main_loop():
    """ボットのメイン実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME
    
    # ... (FX data acquisition logic is the same) ...

    while True:
        try:
            now = time.time()
            
            # 💡 修正: ポジション決済チェック
            if EXCHANGE_CLIENT:
                await check_and_handle_spot_orders()

            if now - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(5)
                continue

            LAST_UPDATE_TIME = now
            
            # 1. 銘柄リストの動的更新
            await update_symbols_by_volume()
            
            # 2. マクロコンテキストの更新 
            crypto_macro = await get_crypto_macro_context()
            GLOBAL_MACRO_CONTEXT.update(crypto_macro)
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, クライアント: {CCXT_CLIENT_NAME} Spot)")

            # 4. 複数銘柄/複数時間足の並行分析
            analysis_tasks = [
                run_multi_timeframe_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            all_results: List[List[Dict]] = await asyncio.gather(*analysis_tasks)
            
            # 5. 結果を平坦化し、LAST_ANALYSIS_SIGNALSを更新
            LAST_ANALYSIS_SIGNALS = [signal for signals in all_results for signal in signals]
            
            # 6. シグナル通知と注文実行
            await notify_signals_in_queue()
            
            # 7. 💡 1時間定期通知のチェック
            if now - LAST_HOURLY_NOTIFICATION_TIME >= HOURLY_NOTIFICATION_INTERVAL:
                 await send_position_status_notification("🕐 1時間 定期ステータス通知")
                 LAST_HOURLY_NOTIFICATION_TIME = now
            
            # 8. 成功時の状態更新
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

app = FastAPI(title="Apex BOT API", version="v19.0.0 - MEXC Spot Trading")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.0 Startup initializing (MEXC Spot Trading)...") 
    
    # CCXT初期化
    await initialize_ccxt_client()
    
    # 💡 初回起動時のステータス通知
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
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.0 - MEXC Spot Trading",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions": len(ACTUAL_POSITIONS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT API is running. Version: v19.0.0 - MEXC Spot Trading"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
