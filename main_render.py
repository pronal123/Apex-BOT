# ====================================================================================
# Apex BOT v17.1.2 - MA/Fundamentals/FGI Enhanced
# 
# 強化ポイント:
# 1. 【移動平均線】SMA 50 (4h) を使用し、長期トレンド逆行時に強力なペナルティ (-0.20) を適用。
# 2. 【ファンダメンタルズ/流動性】板の厚み（オーダーブック深度）を取得し、流動性フィルターとしてスコアリングに導入。
# 3. 【ファンダメンタルズ/出来高】OBV (On-Balance Volume) によるモメンタム確証を追加。
# 4. 【恐怖指数】FGI (Fear & Greed Index) プロキシを導入し、市場センチメントをスコアに反映。
# 5. エリオット波動の概念に基づき、フィボナッチ・ピボット近接度を分析に導入。
# 6. Telegram通知メッセージに「現在単価 (Market Price)」を追加。
# 7. Structural SL使用時に 0.5 * ATR のバッファを追加し、安全性を向上。
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


def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """
    【v17.1.2 改良版】現在単価、長期トレンド、ファンダ/恐怖指数情報を追加した通知メッセージを生成する
    """
    
    valid_signals = [s for s in signals if s.get('side') not in ["DataShortage", "ExchangeError", "Neutral"]]
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

    # --- 取引計画部 ---
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
        f"  - <b>利益確定 (TP)</b>: <code>動的追跡</code> (利益最大化)\n"
        f"  - <b>目標RRR</b>: 1 : {rr_ratio:.2f}+\n\n"
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
        f"<i>Bot Ver: v17.1.2 (MA/Funda/FGI Enhanced)</i>"
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
    """
    価格とPivotポイントを比較し、構造的なSL/TPを決定し、ボーナススコアを返す (0.07点)
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
            
        if price > S1 and (price - S1) / (R1 - S1) < 0.5:
            # Current price is between S1 and the midpoint (closer to S1). Idea: Target R1, SL S1.
            bonus = BONUS_POINT 
            structural_sl = S1
            structural_tp = R1 * 1.01 
        elif price > R1:
            # Price is above R1 (Breakout scenario). Idea: Target R1 extension, SL R1.
            bonus = BONUS_POINT
            structural_sl = R1 
            structural_tp = R1 * 1.05 
            
    elif side == "ショート":
        # 1. フィボナッチレベルの近接度チェック (エントリー精度向上)
        if abs(price - R1) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R1'
        elif abs(price - R2) < FIB_PROXIMITY_THRESHOLD:
            proximity_level = 'R2'
            
        if price < R1 and (R1 - price) / (R1 - S1) < 0.5:
            # Current price is between R1 and the midpoint (closer to R1). Idea: Target S1, SL R1.
            bonus = BONUS_POINT 
            structural_sl = R1 # R1 is the resistance used as SL
            structural_tp = S1 * 0.99 
        elif price < S1:
            # Price is below S1 (Breakout scenario). Idea: Target S1 extension, SL S1.
            bonus = BONUS_POINT
            structural_sl = S1 # S1 is the support used as SL/resistance
            structural_tp = S1 * 0.95 

    # 近接レベルにいる場合はボーナスを強化
    if proximity_level != 'N/A':
         bonus = max(bonus, BONUS_POINT + 0.03) 

    return bonus, structural_sl, structural_tp, proximity_level

async def analyze_single_timeframe(symbol: str, timeframe: str, macro_context: Dict, client_name: str, long_term_trend: str, long_term_penalty_applied: bool) -> Optional[Dict]:
    """
    単一の時間軸で分析とシグナル生成を行う関数 (v17.1.2)
    """
    
    # 1. データ取得とFunding Rate/Order Book取得
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, timeframe)
    
    funding_rate_val = 0.0
    order_book_data = None
    
    if timeframe == '1h': 
        funding_rate_val = await fetch_funding_rate(symbol)
        # 💡 オーダーブック深度を取得
        order_book_data = await fetch_order_book_depth(symbol)
    
    
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
        "funding_rate_value": funding_rate_val, 
        "funding_rate_bonus_value": 0.0, 
        "dynamic_exit_strategy": "DTS",
        "dominance_trend": "Neutral",
        "dominance_bias_bonus_value": 0.0,
        "liquidity_bonus_value": 0.0, 
        "ask_bid_ratio": order_book_data.get('ask_bid_ratio', 1.0) if order_book_data else 1.0,
        "obv_trend_match": "N/A", 
        "obv_momentum_bonus_value": 0.0, 
        "fib_proximity_level": "N/A"
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
    atr_val = df['close'].iloc[-1] * 0.005 if not df.empty and df['close'].iloc[-1] > 0 else 0.005 # 仮のATR値
    
    final_side = "Neutral"
    base_score = BASE_SCORE # 0.40 からスタート
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
        # 💡 OBVを追加
        df['obv'] = ta.obv(df['close'], df['volume']) 
        
        # Pivot Pointの計算 (Structural Analysis)
        pivots = calculate_fib_pivot(df)
        
        # データクリーニング (NaN値の削除)
        required_cols = ['rsi', MACD_HIST_COL, 'adx', 'atr', 'cci', 'vwap', PPO_HIST_COL, 'obv'] 
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
        atr_val = df['atr'].iloc[-1] # 更新
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
        
        # A-I スコアリングロジックの実行 (テクニカル)
        
        # A. MACDに基づく方向性 (0.15)
        if macd_hist_val > 0 and macd_hist_val > macd_hist_val_prev:
            long_score += 0.15 
        elif macd_hist_val < 0 and macd_hist_val < macd_hist_val_prev:
            short_score += 0.15 

        # B. RSIに基づく買われすぎ/売られすぎ (0.08)
        if rsi_val < RSI_OVERSOLD:
            long_score += 0.08
        elif rsi_val > RSI_OVERBOUGHT:
            short_score += 0.08
            
        # C. RSIに基づくモメンタムブレイクアウト (0.12)
        if rsi_val > RSI_MOMENTUM_HIGH and df['rsi'].iloc[-2] <= RSI_MOMENTUM_HIGH:
            long_score += 0.12 
        elif rsi_val < RSI_MOMENTUM_LOW and df['rsi'].iloc[-2] >= RSI_MOMENTUM_LOW:
            short_score += 0.12 

        # D. ADXに基づくトレンドフォロー強化 (0.10)
        if adx_val > ADX_TREND_THRESHOLD:
            if long_score > short_score:
                long_score += 0.10
            elif short_score > long_score:
                short_score += 0.10
        
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
        if ppo_hist_val > 0 and abs(ppo_hist_val) > ppo_abs_mean:
            long_score += 0.04 
        elif ppo_hist_val < 0 and abs(ppo_hist_val) > ppo_abs_mean:
            short_score += 0.04

        # G. Donchian Channelによるブレイクアウト (0.15)
        is_breaking_high = False
        is_breaking_low = False
        if dc_cols_present: 
            is_breaking_high = price > dc_high_val and df['close'].iloc[-2] <= dc_high_val
            is_breaking_low = price < dc_low_val and df['close'].iloc[-2] >= dc_low_val

            if is_breaking_high:
                long_score += 0.15 
            elif is_breaking_low:
                short_score += 0.15
        
        # H. 複合モメンタム加速ボーナス (0.05)
        if macd_hist_val > 0 and ppo_hist_val > 0 and rsi_val > 50:
             long_score += 0.05
        elif macd_hist_val < 0 and ppo_hist_val < 0 and rsi_val < 50:
             short_score += 0.05
             
        # I. CCIに基づくモメンタム加速ボーナス (0.04)
        if cci_val > CCI_OVERBOUGHT and cci_val > df['cci'].iloc[-2]:
             long_score += 0.04
        elif cci_val < CCI_OVERSOLD and cci_val < df['cci'].iloc[-2]:
             short_score += 0.04

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

        # J. 資金調達率 (Funding Rate) バイアスフィルター (+/- 0.08点)
        funding_rate_bonus = 0.0
        
        if timeframe == '1h': 
            if side == "ロング":
                if funding_rate_val > FUNDING_RATE_THRESHOLD: 
                    funding_rate_bonus = -FUNDING_RATE_BONUS_PENALTY
                elif funding_rate_val < -FUNDING_RATE_THRESHOLD: 
                    funding_rate_bonus = FUNDING_RATE_BONUS_PENALTY
            elif side == "ショート":
                if funding_rate_val < -FUNDING_RATE_THRESHOLD: 
                    funding_rate_bonus = -FUNDING_RATE_BONUS_PENALTY
                elif funding_rate_val > FUNDING_RATE_THRESHOLD: 
                    funding_rate_bonus = FUNDING_RATE_BONUS_PENALTY

        score = max(BASE_SCORE, min(1.0, score + funding_rate_bonus))

        # K. BTCドミナンスバイアスフィルター (Altcoinのみ) (+/- 0.05点)
        dominance_bonus = 0.0
        dominance_trend = macro_context.get('dominance_trend', 'Neutral')
        dominance_bias_score_val = macro_context.get('dominance_bias_score', 0.0)
        
        if symbol != "BTC-USDT" and dominance_trend != "Neutral":
            
            if dominance_trend == "Increasing": 
                if side == "ロング":
                    dominance_bonus = dominance_bias_score_val 
                elif side == "ショート":
                    dominance_bonus = abs(dominance_bias_score_val) 
            
            elif dominance_trend == "Decreasing": 
                if side == "ロング":
                    dominance_bonus = abs(dominance_bias_score_val) 
                elif side == "ショート":
                    dominance_bonus = dominance_bias_score_val 
                    
            score = max(BASE_SCORE, min(1.0, score + dominance_bonus))


        # L. 💡【恐怖指数】市場センチメント (FGI Proxy) の適用 (+/-0.07点)
        sentiment_bonus = macro_context.get('sentiment_fgi_proxy', 0.0)
        if side == "ロング" and sentiment_bonus > 0:
            score = min(1.0, score + sentiment_bonus)
        elif side == "ショート" and sentiment_bonus < 0:
            score = min(1.0, score + abs(sentiment_bonus))
        
        # M. Structural/Pivot/Fib Analysis (0.07点 + Fib強化)
        structural_pivot_bonus, structural_sl_pivot, structural_tp_pivot, fib_level = analyze_structural_proximity(price, pivots, side, atr_val)
        score = min(1.0, score + structural_pivot_bonus)
        
        # N. 出来高/流動性確証 & OBV (Max 0.12 + 0.04)
        volume_confirmation_bonus = 0.0
        
        # 出来高ブレイクアウト確証
        if volume_ratio >= VOLUME_CONFIRMATION_MULTIPLIER: 
            if dc_cols_present and (is_breaking_high or is_breaking_low):
                volume_confirmation_bonus += 0.06
            if abs(macd_hist_val) > df[MACD_HIST_COL].abs().mean():
                volume_confirmation_bonus += 0.06
                
            score = min(1.0, score + volume_confirmation_bonus)
            
        # 💡 OBVモメンタム一致のボーナス
        obv_trend_match = "N/A"
        obv_momentum_bonus = 0.0
        if obv_val > obv_prev_val and side == "ロング":
             obv_trend_match = "Long"
             obv_momentum_bonus = OBV_MOMENTUM_BONUS
        elif obv_val < obv_prev_val and side == "ショート":
             obv_trend_match = "Short"
             obv_momentum_bonus = OBV_MOMENTUM_BONUS
             
        score = min(1.0, score + obv_momentum_bonus)
        
        # O. 💡【ファンダメンタルズ】流動性/スマートマネーフィルター (板の厚み) (1h足のみで適用)
        liquidity_bonus = 0.0
        ask_bid_ratio_val = 1.0
        
        if timeframe == '1h' and order_book_data:
            ask_bid_ratio_val = order_book_data.get('ask_bid_ratio', 1.0)
            
            if side == "ロング":
                # Bid側が厚い (買い圧力/流動性あり)
                if ask_bid_ratio_val < 0.9:
                    liquidity_bonus = LIQUIDITY_BONUS_POINT
            elif side == "ショート":
                # Ask側が厚い (売り圧力/流動性あり)
                if ask_bid_ratio_val > 1.1:
                    liquidity_bonus = LIQUIDITY_BONUS_POINT
            
            score = min(1.0, score + liquidity_bonus)
        
        
        # 4. **フィルターによるペナルティ適用**
        
        # P. 💡【移動平均線】4hトレンドフィルターの適用 (15m, 1hのみ) 
        penalty_value_lt = 0.0
        if timeframe in ['15m', '1h']:
            if (side == "ロング" and long_term_trend == "Short") or \
               (side == "ショート" and long_term_trend == "Long"):
                # 長期トレンド逆行時に強力なペナルティを適用
                score = max(BASE_SCORE, score - LONG_TERM_REVERSAL_PENALTY) 
                current_long_term_penalty_applied = True
                penalty_value_lt = LONG_TERM_REVERSAL_PENALTY
        
        # Q. MACDクロス確認と減点 (モメンタム反転チェック)
        macd_valid = True
        penalty_value_macd = 0.0
        if timeframe in ['15m', '1h']:
             is_macd_reversing = (macd_hist_val > 0 and macd_hist_val < macd_hist_val_prev) or \
                                 (macd_hist_val < 0 and macd_hist_val > macd_hist_val_prev)
             
             if is_macd_reversing and score >= SIGNAL_THRESHOLD:
                 score = max(BASE_SCORE, score - MACD_CROSS_PENALTY)
                 macd_valid = False
                 penalty_value_macd = MACD_CROSS_PENALTY
             
        
        # 5. TP/SLとRRRの決定 (Dynamic Trailing Stop & Structural SL)
        
        rr_base = DTS_RRR_DISPLAY 
        
        is_high_conviction = score >= 0.80
        is_strong_trend = adx_val >= 35
        
        # フィボナッチレベルに近接している場合はLimit Entryを推奨
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
            if use_market_entry: 
                entry = price
            else: 
                # Limit Entry (Targeting a low price / Midpoint / Fibonacci Support)
                entry_candidates = [price, bb_mid, dc_mid]
                if fib_level in ['S1', 'S2']: entry_candidates.append(pivots[fib_level])
                entry = min(entry_candidates) 
            
            atr_sl = entry - sl_dist_atr
            
            # Structural SL (S1) を使用する場合は、0.5*ATRだけバッファを下にずらす
            if structural_sl_pivot > 0 and structural_sl_pivot > atr_sl and structural_sl_pivot < entry:
                 sl = structural_sl_pivot - atr_val * 0.5 
                 structural_sl_used = True
            else:
                 sl = atr_sl
            
            # 負の値にならないように保護
            if sl <= 0: sl = entry * 0.99 
            
            tp_dist = abs(entry - sl) * rr_base 
            tp1 = entry + tp_dist
            
        elif side == "ショート":
            if use_market_entry: 
                entry = price
            else: 
                # Limit Entry (Targeting a high price / Midpoint / Fibonacci Resistance)
                entry_candidates = [price, bb_mid, dc_mid]
                if fib_level in ['R1', 'R2']: entry_candidates.append(pivots[fib_level])
                entry = max(entry_candidates) 
            
            atr_sl = entry + sl_dist_atr
            
            # Structural SL (R1) を使用する場合は、0.5*ATRだけバッファを上にずらす
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
        
        # 6. 最終的なサイドの決定
        final_side = side
        if score < SIGNAL_THRESHOLD: 
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
            "sentiment_fgi_proxy_bonus": sentiment_bonus, # 💡 FGIプロキシボーナス値
            "structural_pivot_bonus": structural_pivot_bonus,
            "volume_ratio": volume_ratio,
            "structural_sl_used": structural_sl_used, 
            "long_term_reversal_penalty_value": penalty_value_lt, # 💡 長期トレンドペナルティ値
            "macd_cross_penalty_value": penalty_value_macd,
            "funding_rate_value": funding_rate_val,
            "funding_rate_bonus_value": funding_rate_bonus,
            "dominance_trend": dominance_trend,
            "dominance_bias_bonus_value": dominance_bonus,
            "liquidity_bonus_value": liquidity_bonus, # 💡 流動性ボーナス値
            "ask_bid_ratio": ask_bid_ratio_val, 
            "obv_trend_match": obv_trend_match, 
            "obv_momentum_bonus_value": obv_momentum_bonus, # 💡 OBVボーナス値
            "fib_proximity_level": fib_level, 
            "dynamic_exit_strategy": "DTS" 
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
    
    # 0. 💡【移動平均線】4hトレンドの事前計算
    long_term_trend = 'Neutral'
    ohlcv_4h, status_4h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '4h')
    df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_4h['close'] = pd.to_numeric(df_4h['close'], errors='coerce').astype('float64')
    df_4h['timestamp'] = pd.to_datetime(df_4h['timestamp'], unit='ms', utc=True)
    df_4h.set_index('timestamp', inplace=True)
    
    if status_4h == "Success" and len(df_4h.dropna(subset=['close'])) >= LONG_TERM_SMA_LENGTH:
        try:
            # 💡 SMA 50を計算
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
    
    # MTF スコアリングブーストロジック
    signal_1h_item = next((r for r in results if r and r.get('timeframe') == '1h'), None)
    signal_15m_item = next((r for r in results if r and r.get('timeframe') == '15m'), None)

    if signal_1h_item and signal_15m_item:
        is_1h_strong_signal = signal_1h_item['score'] >= 0.80 
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
            
            # 💡 グローバルマクロコンテキストを更新 (FGI Proxy, BTC/ETHトレンド, Dominance Bias)
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            
            log_symbols = [s.replace('-', '/') for s in monitor_symbols[:5]]
            logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)} - 出来高TOP, クライアント: {CCXT_CLIENT_NAME})。監視リスト例: {', '.join(log_symbols)}...")
            
            results_list_of_lists = []
            
            # 💡 オーダーブックを取得するシンボルを抽出 (1h足で利用)
            ob_fetch_symbols = [s for s in monitor_symbols]
            ob_tasks = [fetch_order_book_depth(symbol) for symbol in ob_fetch_symbols]
            await asyncio.gather(*ob_tasks) # 並行実行してORDER_BOOK_CACHEを更新
            
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
                
                if signal.get('side') == 'Neutral' or signal.get('side') in ["DataShortage", "ExchangeError"]:
                    continue
                
                # Limit Entry の優位性スコアを計算 (EAS)
                entry_advantage_score = 0.0
                atr_val = signal.get('tech_data', {}).get('atr_value', 1.0)
                price = signal.get('price', 0.0)
                entry_price = signal.get('entry', 0.0)
                
                if signal.get('entry_type', 'N/A') == 'Limit' and atr_val > 0 and price > 0 and entry_price > 0:
                    if signal.get('side') == 'ロング':
                        # Limit Long: エントリー価格が現在価格より低い方が優位
                        entry_advantage_score = (price - entry_price) / atr_val
                    elif signal.get('side') == 'ショート':
                        # Limit Short: エントリー価格が現在価格より高い方が優位
                        entry_advantage_score = (entry_price - price) / atr_val


                if symbol not in best_signals_per_symbol or score > best_signals_per_symbol[symbol]['score']:
                    all_symbol_signals = [s for s in all_signals if s['symbol'] == symbol]
                    
                    best_signals_per_symbol[symbol] = {
                        'score': score, 
                        'all_signals': all_symbol_signals,
                        'rr_ratio': signal.get('rr_ratio', 0.0), 
                        'adx_val': signal.get('tech_data', {}).get('adx', 0.0), 
                        'atr_val': atr_val,
                        'symbol': symbol,
                        'entry_type': signal.get('entry_type', 'N/A'),
                        'entry_advantage_score': entry_advantage_score
                    }
            
            # --- Limit Entry ポジションのみをフィルタリングし、ソート基準を優位性スコアに変更 ---
            limit_entry_signals = [
                item for item in best_signals_per_symbol.values() 
                if item['entry_type'] == 'Limit' 
            ]

            # ソート: Entry Advantage Score (EAS, 優位性) を最優先（深いリトレースメントを狙う）、次にスコア、RRRの順
            sorted_best_signals = sorted(
                limit_entry_signals, 
                key=lambda x: (
                    x['entry_advantage_score'], # EASを最優先 (降順)
                    x['score'],     
                    x['rr_ratio'],  
                    x['adx_val'],   
                    x['symbol']     
                ), 
                reverse=True
            )
            # --------------------------------------------------------------------------
            
            top_signals_to_notify = [
                item for item in sorted_best_signals 
                if item['score'] >= SIGNAL_THRESHOLD
            ][:TOP_SIGNAL_COUNT]
            
            notify_tasks = [] 
            
            if top_signals_to_notify:
                logging.info(f"🔔 高スコア/高優位性シグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['all_signals'][0]['symbol']
                    current_time = time.time()
                    
                    if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                        
                        msg = format_integrated_analysis_message(symbol, item['all_signals'], i + 1)
                        
                        if msg:
                            log_symbol = symbol.replace('-', '/')
                            logging.info(f"📰 通知タスクをキューに追加: {log_symbol} (順位: {i+1}位, スコア: {item['score'] * 100:.2f}点, EAS: {item['entry_advantage_score']:.2f})")
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

app = FastAPI(title="Apex BOT API", version="v17.1.2 - MA/Funda/FGI Enhanced")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v17.1.2 Startup initializing...") 
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
        "bot_version": "v17.1.2 - MA/Funda/FGI Enhanced",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running (v17.1.2, MA/Funda/FGI Enhanced)."}, status_code=200)

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
