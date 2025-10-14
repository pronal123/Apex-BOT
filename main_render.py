# ====================================================================================
# Apex BOT v19.0.5 - Spot Trading & Position Management Implementation (Data Integrity Fix)
# 
# 強化ポイント (v19.0.4からの変更):
# 1. 【データ完全性チェック】analyze_single_timeframe内で、テクニカル指標計算後の df.dropna() 処理後に、
#    データフレームの行数が30本未満になった場合に警告を出し、分析をスキップするチェックを追加。
#    これにより、データ不足によるATR_14計算失敗の警告をより明確に処理します。
# 2. 【バージョン更新】全てのバージョン情報を v19.0.5 に更新。
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
import yfinance as yf # 実際には未使用だが、一般的な金融BOTのセットアップとしてインポート
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
TOP_SYMBOL_LIMIT = 30      # 出来高上位30銘柄を監視
LOOP_INTERVAL = 180        # メインループの実行間隔（秒）
REQUEST_DELAY_PER_SYMBOL = 0.5 # 銘柄ごとのAPIリクエストの遅延（秒）

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.75             # シグナルを通知する最低スコア
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 # ボリンジャーバンドの幅が狭い場合のペナルティ閾値 (%)

LONG_TERM_SMA_LENGTH = 50           # 長期トレンド判定に使用するSMAの期間（4h足）
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンドと逆行する場合のスコアペナルティ
MACD_CROSS_PENALTY = 0.15           # MACDが有利なクロスでない場合のペナルティ

ATR_TRAIL_MULTIPLIER = 3.0          # ATRに基づいた初期SL/TPの乗数
DTS_RRR_DISPLAY = 5.0               # 通知メッセージに表示するリスクリワード比率

LIQUIDITY_BONUS_POINT = 0.06        # 板の厚み（流動性）ボーナス
ORDER_BOOK_DEPTH_LEVELS = 5         # オーダーブックの取得深度
OBV_MOMENTUM_BONUS = 0.04           # OBVによるモメンタム確証ボーナス
FGI_PROXY_BONUS_MAX = 0.07          # FGIプロキシによる最大ボーナス

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
RSI_MOMENTUM_HIGH = 60              # RSIが60以上でショートモメンタム候補
ADX_TREND_THRESHOLD = 30            # ADXによるトレンド/レンジ判定
BASE_SCORE = 0.40                   # ベースとなるスコア
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 # 出来高が過去平均のX倍以上で確証

# 💡 自動売買設定
MAX_RISK_PER_TRADE_USDT = 5.0       # 1取引あたりの最大リスク額 (USDT)
MAX_RISK_CAPITAL_PERCENT = 0.01     # 1取引あたりの最大リスク額 (総資金に対する割合)
TRADE_SIZE_PER_RISK_MULTIPLIER = 1.0 # 許容リスク額に対する取引サイズ乗数（1.0でリスク額＝損失額）
MIN_USDT_BALANCE_TO_TRADE = 50.0    # 取引を開始するための最低USDT残高

# ====================================================================================
# GLOBAL STATE & CACHES
# ====================================================================================

CCXT_CLIENT_NAME: str = 'MEXC' 
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT] 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} # 通知済みシグナルのクールダウン管理
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_SUCCESS_TIME: float = 0.0
LAST_SUCCESSFUL_MONITOR_SYMBOLS: List[str] = CURRENT_MONITOR_SYMBOLS.copy()
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} # 流動性データキャッシュ

# 💡 v18.0.3 ポジション管理システム
# {symbol: {'entry_price': float, 'amount': float, 'sl_price': float, 'tp_price': float, 'open_time': float, 'status': str}}
ACTUAL_POSITIONS: Dict[str, Dict] = {} 
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0

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
    """時間足に応じたTP到達までの目安時間を返す"""
    if timeframe == '15m': return "数時間〜半日"
    if timeframe == '1h': return "半日〜数日"
    if timeframe == '4h': return "数日〜1週間"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の桁数を調整してフォーマットする"""
    if price < 0.0001: return f"{price:.8f}"
    if price < 0.01: return f"{price:.6f}"
    if price < 1.0: return f"{price:.4f}"
    if price < 100.0: return f"{price:,.2f}"
    return f"{price:,.2f}"

def format_usdt(amount: float) -> str:
    """USDT残高をフォーマットする"""
    return f"{amount:,.2f}"

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間足から推定勝率を算出する"""
    # 0.40(ベース)で58%、1.00で80%程度になるよう調整
    base_rate = score * 0.50 + 0.35
    
    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate))
    elif timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    elif timeframe == '4h':
        return max(0.50, min(0.90, base_rate))
    return base_rate

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """分析結果を統合したTelegramメッセージをHTML形式で作成する"""
    valid_signals = [s for s in signals if s.get('side') == 'ロング'] 
    if not valid_signals:
        return "" 
        
    # スコアが閾値を超えたシグナルの中から、最もRRR/スコアが高いものを選択
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals:
        return "" 
        
    best_signal = max(
        high_score_signals, 
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )
    
    # 💡 v18.0.3: 取引サイズとリスク額を表示
    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    
    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    tp1_price = best_signal.get('tp1', 0.0)
    
    trade_plan_data = best_signal.get('trade_plan', {})
    trade_amount_usdt = trade_plan_data.get('trade_size_usdt', 0.0)
    max_risk_usdt = trade_plan_data.get('max_risk_usdt', MAX_RISK_PER_TRADE_USDT)
    
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
    direction_text = "<b>ロング (現物買い推奨)</b>"
        
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
        f"<b>✅ 自動取引計画</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>取引サイズ (USDT)</b>: <code>{format_usdt(trade_amount_usdt)}</code>\n"
        f"  - <b>許容最大リスク</b>: <code>${format_usdt(max_risk_usdt)}</code>\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>参考損切り (SL)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>参考利確 (TP)</b>: <code>${format_price_utility(tp1_price, symbol)}</code> (DTS Base)\n"
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
        f"  - <b>決済までの目安</b>: {get_tp_reach_time(timeframe)}\n"
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
        f"<pre>※ このシグナルは自動売買の対象です。</pre>"
        f"<i>Bot Ver: v19.0.5 (Data Integrity Fix)</i>" 
    )

    return header + trade_plan + summary + analysis_details + footer

def format_position_status_message(balance_usdt: float, open_positions: Dict) -> str:
    """現在のポジション状態をまとめたTelegramメッセージをHTML形式で作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    header = (
        f"🔔 **Apex BOT ポジション/残高ステータス ({CCXT_CLIENT_NAME} Spot)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **最終確認日時**: {now_jst} (JST)\n"
        f"  - **利用可能USDT残高**: <code>${format_usdt(balance_usdt)}</code>\n"
        f"  - **保有中ポジション数**: <code>{len(open_positions)}</code> 件\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )
    
    if not open_positions:
        return header + "👉 **現在、保有中の現物ポジションはありません。**\n"
    
    details = "📈 **保有ポジション詳細**\n\n"
    for symbol, pos in open_positions.items():
        entry = format_price_utility(pos['entry_price'], symbol)
        sl = format_price_utility(pos['sl_price'], symbol)
        tp = format_price_utility(pos['tp_price'], symbol)
        amount = pos['amount']
        
        details += (
            f"🔹 <b>{symbol}</b> ({amount:.4f} 単位)\n"
            f"  - Buy @ <code>${entry}</code> (Open: {datetime.fromtimestamp(pos['open_time'], tz=JST).strftime('%m/%d %H:%M')})\n"
            f"  - SL: <code>${sl}</code> | TP: <code>${tp}</code>\n"
            f"  - Status: {pos['status']}\n"
        )
        
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.5</i>"
    )
    
    return header + details + footer

def send_telegram_html(message: str):
    """TelegramにHTML形式でメッセージを送信する"""
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


async def fetch_current_balance_usdt() -> float:
    """CCXTから現在のUSDT残高を取得する。"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        # 認証情報がない場合、この呼び出しは失敗する
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # SpotアカウントのUSDT残高を取得 (freeを使用)
        usdt_free = balance.get('USDT', {}).get('free', 0.0)
        
        if usdt_free == 0.0 and 'USDT' not in balance.get('total', {}):
             # NOTE: totalにUSDTがあってもfreeが0なら取引不能だが、キーが見つからない場合をエラーとして明確に区別する
             if 'USDT' not in balance:
                raise Exception("残高情報に'USDT'キーが見つからず、他のどの通貨の残高も確認できません。APIキー/Secretの**入力ミス**または**Spot残高読み取り権限**を再度確認してください。")
             
        return usdt_free
        
    except Exception as e:
        logging.error(f"残高取得エラー（キー/権限不備）: {e}")
        return 0.0


async def update_symbols_by_volume():
    """出来高TOP銘柄を更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        await EXCHANGE_CLIENT.load_markets() 
        
        usdt_tickers = {}
        # NOTE: 出来高ベースでの銘柄選定は、銘柄数が多いため`fetch_tickers`で一括取得するのが効率的だが、
        # MEXCのレート制限を避けるため、ここでは`fetch_ticker`をシンボルごとに実行する簡易版を採用
        
        spot_usdt_symbols = [
             symbol for symbol, market in EXCHANGE_CLIENT.markets.items()
             if market['active'] and market['quote'] == 'USDT' and market['spot']
        ]

        # 全てのシンボルをチェックすると時間がかかるため、DEFAULT_SYMBOLSと合わせてチェック
        symbols_to_check = list(set(DEFAULT_SYMBOLS + spot_usdt_symbols))
        
        # 出来高データの取得
        for symbol in symbols_to_check:
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
    """OHLCVデータを取得する"""
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
        #logging.warning(f"OHLCV取得エラー ({symbol} {timeframe}): {e}")
        return [], "ExchangeError", client_name

async def get_crypto_macro_context() -> Dict:
    """FGIプロキシ (BTC/ETHの4h足SMA50トレンド) を計算する"""
    btc_ohlcv, status_btc, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC/USDT", '4h')
    eth_ohlcv, status_eth, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "ETH/USDT", '4h')

    btc_trend = 0
    eth_trend = 0
    
    # BTCトレンド判定
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

    # ETHトレンド判定
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

    # FGIプロキシスコア計算
    sentiment_score = 0.0
    if btc_trend == 1 and eth_trend == 1:
        # 両方ロングトレンド（リスクオン）
        sentiment_score = FGI_PROXY_BONUS_MAX
    elif btc_trend == -1 and eth_trend == -1:
        # 両方ショートトレンド（リスクオフ）
        sentiment_score = -FGI_PROXY_BONUS_MAX
        
    return {
        "btc_trend_4h": "Long" if btc_trend == 1 else ("Short" if btc_trend == -1 else "Neutral"),
        "eth_trend_4h": "Long" if eth_trend == 1 else ("Short" if eth_trend == -1 else "Neutral"),
        "sentiment_fgi_proxy": sentiment_score,
        'fx_bias': 0.0
    }
    
async def fetch_order_book_depth(symbol: str) -> Optional[Dict]:
    """オーダーブックの流動性深度を取得する"""
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
        # logging.warning(f"{symbol} のオーダーブック取得エラー: {e}")
        return None

# ====================================================================================
# CORE ANALYSIS & TRADE EXECUTION LOGIC (v19.0.5 - Data Integrity Fix applied)
# ====================================================================================

def analyze_structural_proximity(df: pd.DataFrame, price: float, side: str) -> Tuple[float, float, bool, str]:
    """構造的な支持/抵抗線の近接度を分析し、SL価格とボーナスを計算する"""
    
    # 💡 v19.0.4 修正: ATRの再計算を削除し、analyze_single_timeframeで計算された値に安全にアクセス
    if 'ATR_14' in df.columns and df['ATR_14'].iloc[-1] > 0 and not df['ATR_14'].isna().iloc[-1]:
        atr = df['ATR_14'].iloc[-1]
    else:
        # ATRが計算できない場合のフォールバック（致命的エラーを防ぐ）
        atr = 0.001 
        
    # 20期間の最安値（ロングの構造的SL候補）
    pivot_long = df['low'].rolling(window=20).min().iloc[-1]
    
    structural_sl_used = False
    
    if side == 'ロング':
        structural_sl_candidate = pivot_long
        
        if structural_sl_candidate > 0:
             # 構造的サポート（ピボット）をSLの基準とし、0.5 ATRのバッファを持たせる
             sl_price = structural_sl_candidate - (0.5 * atr) 
             structural_sl_used = True
        else:
             # 構造的候補がない場合はATR基準
             sl_price = price - (ATR_TRAIL_MULTIPLIER * atr)
             
    else: 
        # ショートシグナルの場合は、いったんATR基準SLを返す
        return 0.0, price + (ATR_TRAIL_MULTIPLIER * atr), False, 'N/A' # ショートの場合はSLは上

    bonus = 0.0
    fib_level = 'N/A'
    
    if side == 'ロング':
        # 現在価格が構造的サポート（pivot_long）に近接しているかチェック
        distance = price - pivot_long
        
        if 0 < distance <= 2.5 * atr:
            # 2.5 ATR以内に重要なサポートがある場合ボーナス
            bonus += 0.08 
            fib_level = "Support Zone"
            
        # 4h足SMA50 (SMA) に価格が近接しているかチェック (トレンドと一致する押し目買いの確認)
        sma_long = df['sma'].iloc[-1] if 'sma' in df.columns and not df['sma'].isna().iloc[-1] else None
        if sma_long and price >= sma_long and price - sma_long < 3 * atr:
            bonus += 0.05
            fib_level += "/SMA50"

    # SL価格が0以下の場合は、現在の価格-1ティックを返す
    if sl_price <= 0:
        sl_price = price * 0.99 

    return bonus, sl_price, structural_sl_used, fib_level


def analyze_single_timeframe(df_ohlcv: List[List[float]], timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """単一時間足のテクニカル分析を実行する"""
    if not df_ohlcv or len(df_ohlcv) < REQUIRED_OHLCV_LIMITS.get(timeframe, 500):
        return None

    df = pd.DataFrame(df_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce').astype('float64')
    df['high'] = pd.to_numeric(df['high'], errors='coerce').astype('float64')
    df['low'] = pd.to_numeric(df['low'], errors='coerce').astype('float64')
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').astype('float64')
    
    # テクニカル指標の計算
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.adx(append=True)
    df.ta.stoch(append=True)
    df.ta.cci(append=True)
    df.ta.bbands(append=True) 
    df.ta.atr(length=14, append=True) # <- ATR_14 を作成
    df.ta.obv(append=True) 
    df['sma'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    df.dropna(inplace=True)
    
    # 🌟 v19.0.5 修正ポイント: dropna後の行数チェックを強化 🌟
    # ATRなどの計算に最低限必要なデータ行数をチェック
    REQUIRED_ROWS_AFTER_NAN = 30 
    if len(df) < REQUIRED_ROWS_AFTER_NAN:
        logging.warning(f"⚠️ {symbol} {timeframe}: dropna後にデータが{len(df)}行しか残りませんでした。分析をスキップします。")
        return None
        
    # ATR_14の存在と有効性をチェック (v19.0.4の修正箇所)
    if 'ATR_14' not in df.columns or df['ATR_14'].iloc[-1] <= 0 or df['ATR_14'].isna().iloc[-1]:
        # この警告が出た場合、データフレームは存在するがATR_14の計算が失敗したことを意味する
        logging.warning(f"⚠️ {symbol} {timeframe}: ATR_14の計算に失敗しました (有効なデータフレームあり)。この時間足の分析をスキップします。")
        return None

    latest = df.iloc[-1]
    price = latest['close']
    
    rsi = latest['RSI_14']
    macd_hist = latest['MACDh_12_26_9']
    adx = latest['ADX_14']
    stoch_k = latest['STOCHk_14_3_3']
    stoch_d = latest['STOCHd_14_3_3']
    cci = latest['CCI_14_0.015']
    atr = latest['ATR_14'] # 安全にアクセス
    volume = latest['volume']
    obv = latest['OBV']
    sma_long = latest['sma']
    
    score = BASE_SCORE 
    side = None
    tech_data = {}
    
    # ロングシグナル判定ロジック
    if rsi < RSI_MOMENTUM_LOW and cci < 0: 
        side = 'ロング'
        
        # 強力な売られ過ぎ（逆張りボーナス）
        if rsi < RSI_OVERSOLD and stoch_k < 30: 
            score += 0.15 
            
        # MACDの上昇モメンタム確認
        if macd_hist > 0 and latest['MACD_12_26_9'] > latest['MACDs_12_26_9']:
            score += 0.10 
            tech_data['macd_cross_valid'] = True
        else:
            tech_data['macd_cross_valid'] = False
            score -= MACD_CROSS_PENALTY 
            
        # 出来高による確証
        if volume > df['volume'].rolling(window=20).mean().iloc[-2] * VOLUME_CONFIRMATION_MULTIPLIER:
             score += 0.08 
             tech_data['volume_confirmation_bonus'] = 0.08
        else:
             tech_data['volume_confirmation_bonus'] = 0.0

    # ショートシグナルはここではスキップ（現物BOTのため）
    elif rsi > RSI_MOMENTUM_HIGH and cci > 0:
         side = 'ショート'
         

    if side == 'ロング':
        # 💡 長期トレンド（4h SMA 50）との比較
        if timeframe == '4h' and sma_long and price < sma_long:
            # 4h足で長期トレンド（SMA 50）が下降中（価格が下）なのにロングシグナルの場合
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_trend'] = "Short"
        else:
            tech_data['long_term_reversal_penalty'] = False
            tech_data['long_term_trend'] = "Long" if sma_long and price >= sma_long else "Neutral"
            
        # ストキャスティクスによる過熱感フィルタ
        if stoch_k > 90 or stoch_d > 90:
            score -= 0.10
            tech_data['stoch_filter_penalty'] = 0.10
        else:
            tech_data['stoch_filter_penalty'] = 0.0
            
        # 低ボラティリティフィルタ（レンジ相場の回避）
        bb_width = latest['BBU_5_2.0'] / latest['BBL_5_2.0'] if 'BBL_5_2.0' in df.columns and latest['BBL_5_2.0'] > 0 else 1.0
        if (bb_width - 1.0) * 100 < VOLATILITY_BB_PENALTY_THRESHOLD:
            score -= 0.05 
            tech_data['volatility_bb_penalty'] = 0.05
        else:
            tech_data['volatility_bb_penalty'] = 0.0
            
        # 💡 OBVモメンタム確認
        obv_sma = df['OBV'].rolling(window=20).mean().iloc[-2]
        if obv > obv_sma:
            score += OBV_MOMENTUM_BONUS
            tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        else:
            tech_data['obv_momentum_bonus_value'] = 0.0

        # SL/TPの計算と構造的サポートボーナス
        struct_bonus, sl_price, structural_sl_used, fib_level = analyze_structural_proximity(df, price, side)
        score += struct_bonus
        tech_data['structural_pivot_bonus'] = struct_bonus
        
        risk_dist = price - sl_price
        if risk_dist <= 0: 
            return None 
        
        # TP価格をリスクリワード比率に基づいて決定
        tp1_price = price + (risk_dist * DTS_RRR_DISPLAY)
        
        # 💡 流動性ボーナス
        ob_data = ORDER_BOOK_CACHE.get(symbol)
        if ob_data:
            if ob_data['bids_usdt'] > ob_data['asks_usdt'] * 1.5:
                score += LIQUIDITY_BONUS_POINT
                tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_POINT
            else:
                 tech_data['liquidity_bonus_value'] = 0.0
        else:
             tech_data['liquidity_bonus_value'] = 0.0
             
        # 💡 FGIプロキシボーナス
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
    """複数時間足の分析を並行して実行する"""
    signals = []
    
    # オーダーブックの流動性データを取得 (ファンダメンタルズ要因)
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

def calculate_trade_size(balance_usdt: float, price: float, sl_price: float) -> Tuple[float, float]:
    """許容リスクに基づき、取引サイズ（数量とUSDT額）を計算する"""
    
    # 1. 許容最大リスク額を決定
    risk_from_capital = balance_usdt * MAX_RISK_CAPITAL_PERCENT
    max_risk_usdt = min(MAX_RISK_PER_TRADE_USDT, risk_from_capital)
    
    if balance_usdt < MIN_USDT_BALANCE_TO_TRADE:
        return 0.0, 0.0 # 取引開始の最低残高を満たさない
    
    # 2. SLまでのリスク距離 (USDT/単位) を計算
    risk_per_unit = price - sl_price
    
    if risk_per_unit <= 0.0:
        logging.warning("⚠️ SL価格がエントリー価格を上回っているため、取引サイズを計算できません。")
        return 0.0, 0.0
        
    # v18.0.3では、リスク額をUSDT建てとし、そのリスク額を元に取引サイズ（USDT）を計算
    # 簡易的に、リスク額のX倍を取引サイズUSDTとする（RRRの目標値を使用）
    trade_size_usdt = max_risk_usdt * DTS_RRR_DISPLAY / 2 
    
    # USDT建ての取引サイズから、実際に購入する単位数量を計算
    amount_unit = trade_size_usdt / price
    
    # 資金の確認
    if trade_size_usdt > balance_usdt:
        trade_size_usdt = balance_usdt * 0.95 # 残高の95%を上限とする
        amount_unit = trade_size_usdt / price
        
    # 最低取引サイズ確認 (ここでは最低20 USDTとする)
    if trade_size_usdt < 20: 
        return 0.0, 0.0
        
    return amount_unit, trade_size_usdt # amount_unit: 数量, trade_size_usdt: USDTでの取引サイズ


async def execute_spot_order(symbol: str, amount_unit: float, price: float, sl_price: float, tp_price: float):
    """現物取引（ロングエントリー）を実行し、ポジションを記録する"""
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    if not EXCHANGE_CLIENT:
        return False
        
    try:
        # 1. 買い注文（Market Order）を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount_unit,
            params={}
        )
        
        # 2. 注文が通ったら、ポジションを管理リストに追加
        # 実際のエントリー価格は取引所から返されたorder['price']を使うのが理想だが、v18.0.3ではシグナル価格を使用
        entry_price = price
        
        # 注文の成功ログ
        logging.info(f"✅ SPOT BUY executed: {symbol} - {amount_unit:.4f} units @ ${format_price_utility(entry_price, symbol)}")
        
        # 3. ポジション管理リストに追加
        ACTUAL_POSITIONS[symbol] = {
            'entry_price': entry_price,
            'amount': amount_unit,
            'sl_price': sl_price,
            'tp_price': tp_price,
            'open_time': time.time(),
            'status': 'Open'
        }
        
        # 4. SL/TP指値注文の発注 (ccxtがStop Limitなどをサポートしていないため、通知のみ)
        logging.warning(f"⚠️ {symbol}: SL/TP注文の発注はBOT内部で監視します。CCXTによる指値注文は実行されません。")
        
        return True
        
    except Exception as e:
        logging.error(f"❌ SPOT BUY Order failed for {symbol}: {e}")
        return False


async def execute_close_position(symbol: str, close_price: float):
    """ポジションを決済（現物売り注文）する"""
    global EXCHANGE_CLIENT, ACTUAL_POSITIONS
    
    if symbol not in ACTUAL_POSITIONS:
        return
        
    pos = ACTUAL_POSITIONS[symbol]
    amount_to_sell = pos['amount']
    
    try:
        # 1. 売り注文（Market Order）を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount_to_sell,
            params={}
        )
        
        # 2. 決済ログとポジションの削除
        logging.info(f"💰 POSITION CLOSED: {symbol} - SELL executed: {amount_to_sell:.4f} units @ ${format_price_utility(close_price, symbol)}")
        
        # 決済通知 (ここでは簡易通知)
        send_telegram_html(f"💰 **決済完了通知** 💰\n"
                           f"<b>{symbol}</b>: ポジションを決済しました。\n"
                           f"  - 決済価格: <code>${format_price_utility(close_price, symbol)}</code>\n"
                           f"  - 理由: SL/TPまたは手動決済")
                           
        del ACTUAL_POSITIONS[symbol]
        
    except Exception as e:
        logging.error(f"❌ SPOT SELL Order failed for {symbol}: {e}")


async def process_signal_and_trade(signal: Dict, balance_usdt: float):
    """シグナル処理と自動取引の実行"""
    symbol = signal['symbol']
    
    # 1. ポジションチェック
    if symbol in ACTUAL_POSITIONS:
        logging.info(f"ℹ️ {symbol}: 既にポジション保有中。新規取引をスキップします。")
        return
        
    # 2. 取引サイズ計算
    amount_unit, trade_size_usdt = calculate_trade_size(balance_usdt, signal['price'], signal['sl'])
    
    if amount_unit <= 0.0:
        logging.warning(f"⚠️ {symbol}: 取引サイズが小さすぎるため、または残高不足のためスキップします。")
        return
        
    # 3. 取引計画をシグナルに組み込み（通知用）
    signal['trade_plan'] = {
        'trade_size_usdt': trade_size_usdt,
        'max_risk_usdt': balance_usdt * MAX_RISK_CAPITAL_PERCENT,
    }
    
    # 4. 取引実行
    success = await execute_spot_order(
        symbol=symbol,
        amount_unit=amount_unit,
        price=signal['entry'],
        sl_price=signal['sl'],
        tp_price=signal['tp1']
    )
    
    if success:
        # 5. Telegram通知
        message = format_integrated_analysis_message(symbol, [signal], 1) # 自動取引実行時は常にRank 1として通知
        send_telegram_html(message)
        
        # クールダウンリストに追加
        TRADE_NOTIFIED_SYMBOLS[symbol] = time.time()


async def check_and_close_positions(current_prices: Dict[str, float]):
    """オープンポジションのSL/TP条件をチェックし、決済を実行する"""
    global ACTUAL_POSITIONS
    
    # 決済が必要なポジションを一時的に保持
    symbols_to_close = []
    
    for symbol, pos in ACTUAL_POSITIONS.items():
        current_price = current_prices.get(symbol)
        
        if not current_price:
            continue
            
        # 1. SLチェック (価格がSL価格以下になった場合)
        if current_price <= pos['sl_price']:
            logging.warning(f"🚨 SL HIT for {symbol}: Current Price ${current_price} <= SL ${pos['sl_price']}")
            symbols_to_close.append((symbol, current_price))
            continue
            
        # 2. TPチェック (価格がTP価格以上になった場合)
        if current_price >= pos['tp_price']:
            logging.info(f"🎉 TP HIT for {symbol}: Current Price ${current_price} >= TP ${pos['tp_price']}")
            symbols_to_close.append((symbol, current_price))
            continue

    # 決済の実行
    for symbol, close_price in symbols_to_close:
        await execute_close_position(symbol, close_price)


async def send_position_status_notification(title: str = "Hourly Status Update"):
    """ポジションと残高のステータスをTelegramに送信する"""
    balance_usdt = await fetch_current_balance_usdt()
    message = format_position_status_message(balance_usdt, ACTUAL_POSITIONS)
    send_telegram_html(f"**{title}**\n\n" + message)


async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME
    
    while True:
        try:
            now = time.time()
            
            if now - LAST_UPDATE_TIME < LOOP_INTERVAL:
                await asyncio.sleep(5)
                continue

            LAST_UPDATE_TIME = now
            
            # 1. 残高とマクロ環境の取得
            balance_usdt = await fetch_current_balance_usdt()
            if balance_usdt < MIN_USDT_BALANCE_TO_TRADE:
                logging.warning(f"⚠️ USDT残高が不足しています ({balance_usdt:.2f} < {MIN_USDT_BALANCE_TO_TRADE:.2f})。取引をスキップし、監視のみ実行します。")
                
            crypto_macro = await get_crypto_macro_context()
            GLOBAL_MACRO_CONTEXT.update(crypto_macro)
            
            # 2. 出来高による監視銘柄の更新
            await update_symbols_by_volume()
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, USDT残高: {balance_usdt:.2f})")

            # 3. 複数銘柄の分析を並列実行
            analysis_tasks = [
                run_multi_timeframe_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            
            all_results: List[List[Dict]] = await asyncio.gather(*analysis_tasks)
            LAST_ANALYSIS_SIGNALS = [signal for signals in all_results for signal in signals]
            
            # 4. 現在価格の取得（ポジション決済チェック用）
            tickers = await EXCHANGE_CLIENT.fetch_tickers(CURRENT_MONITOR_SYMBOLS + list(ACTUAL_POSITIONS.keys()))
            current_prices = {s: t['last'] for s, t in tickers.items() if t and t.get('last')}
            
            # 5. ポジションの決済チェック
            await check_and_close_positions(current_prices)
            
            # 6. 新規シグナルの処理と取引実行
            high_conviction_signals = sorted(
                [sig for sig in LAST_ANALYSIS_SIGNALS if sig['score'] >= SIGNAL_THRESHOLD and sig['side'] == 'ロング' and sig['symbol'] not in TRADE_NOTIFIED_SYMBOLS],
                key=lambda x: (x['score'], x['rr_ratio']), reverse=True
            )
            
            signals_to_act = high_conviction_signals[:TOP_SIGNAL_COUNT]
            
            for signal in signals_to_act:
                if balance_usdt >= MIN_USDT_BALANCE_TO_TRADE:
                    await process_signal_and_trade(signal, balance_usdt)
                else:
                    # 取引を実行しない場合でも通知クールダウンリストには追加しておく
                    TRADE_NOTIFIED_SYMBOLS[signal['symbol']] = now
                    # 通知メッセージの送信 (取引サイズ計算部分は省略)
                    # ここでは、format_integrated_analysis_messageを呼び出す前にtrade_planデータ構造が必須
                    # ダミーデータを作成して通知
                    signal['trade_plan'] = {
                        'trade_size_usdt': 0.0,
                        'max_risk_usdt': balance_usdt * MAX_RISK_CAPITAL_PERCENT,
                    }
                    message = format_integrated_analysis_message(signal['symbol'], [signal], 1)
                    message = message.replace("✅ 自動取引計画", "⚠️ 通知のみ（残高不足）")
                    message = message.replace("<code>0.00</code>", "<code>不足</code>")
                    send_telegram_html(message)

            # 7. Hourly Status Notification
            if now - LAST_HOURLY_NOTIFICATION_TIME >= 60 * 60:
                 await send_position_status_notification()
                 LAST_HOURLY_NOTIFICATION_TIME = now

            LAST_SUCCESS_TIME = now
            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.5)。次の分析まで {LOOP_INTERVAL} 秒待機。")

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

app = FastAPI(title="Apex BOT API", version="v19.0.5 - Data Integrity Fix") 

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.5 Startup initializing (Data Integrity Fix)...") 
    
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
        "bot_version": "v19.0.5 - Data Integrity Fix",
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
    return JSONResponse(content={"message": f"Apex BOT API is running. Version: v19.0.5 - Data Integrity Fix"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    # uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
    pass
