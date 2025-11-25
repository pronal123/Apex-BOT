# ====================================================================================
# Apex BOT v19.0.55 - HOTFIX: Bybit Geo-Block Avoidance
#
# 改良・修正点 (v19.0.55):
# 1. 【ジオブロック回避】initialize_exchange_client 関数内のCCXTオプションを修正。
#    `'loadMarkets': {'spot': False, 'option': False}` を追加し、
#    デプロイ環境における Bybit API の 403 Forbidden (CloudFront geo-block) エラーを回避。
# 2. 【マクロトレンド維持】v19.0.54の長期トレンド分析ロジックを維持。
# 3. 【Syntax Fix維持】v19.0.54の global 宣言順序修正を維持。
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
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - (%(threadName)s) - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout) # 標準出力にも出力
    ]
)

# ====================================================================================
# CONSTANTS & SETTINGS
# ====================================================================================

# BOTのバージョン情報
BOT_VERSION = "v19.0.55" 
JST = timezone(timedelta(hours=+9), 'JST') # 日本時間 (JST)

# 取引所設定
EXCHANGE_ID = os.environ.get('EXCHANGE_ID', 'bybit')
API_KEY = os.environ.get('API_KEY')
SECRET = os.environ.get('SECRET')
DEFAULT_SYMBOLS: List[str] = os.environ.get('SYMBOLS', 'BTC/USDT:USDT,ETH/USDT:USDT').split(',')

# BOTの動作設定
TEST_MODE = os.environ.get('TEST_MODE', 'True').lower() == 'true'
LOOP_INTERVAL = int(os.environ.get('LOOP_INTERVAL', 60)) # メインループ間隔（秒）
MONITOR_INTERVAL = int(os.environ.get('MONITOR_INTERVAL', 10)) # 注文監視ループ間隔（秒）
POSITION_SIZE_USDT = float(os.environ.get('POSITION_SIZE_USDT', 50.0)) # 1回あたりの取引サイズ (USDT)
LEVERAGE = int(os.environ.get('LEVERAGE', 10)) # レバレッジ
FEE_RATE = float(os.environ.get('FEE_RATE', 0.0006)) # Taker Fee Rate (0.06% = 0.0006)

# テレグラム設定 (通知用)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
NOTIFICATION_ENABLED = TELEGRAM_TOKEN and TELEGRAM_CHAT_ID

# 取引戦略設定
# ATR (Average True Range) ベースのSL/TP設定
ATR_PERIOD = 14
ATR_MULTIPLIER_SL = 1.0 # ストップロス倍率 (1.0 * ATR)
ATR_MULTIPLIER_TP = 1.5 # テイクプロフィット倍率 (1.5 * ATR)
RE_ADJUST_SL_TP_ENABLED = os.environ.get('RE_ADJUST_SL_TP_ENABLED', 'True').lower() == 'true' # SL/TP再設定機能の有効化

# RSI設定
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# MACD設定
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# シグナルスコアリング設定 (0から100点)
RSI_SCORE_WEIGHT = 40
MACD_SCORE_WEIGHT = 60
TOTAL_SCORE_MAX = RSI_SCORE_WEIGHT + MACD_SCORE_WEIGHT

# マクロ環境に基づく動的取引閾値設定
SIGNAL_THRESHOLD_SLUMP = 0.88 # リスクオフ時の厳しめの閾値 (88点)
SIGNAL_THRESHOLD_NORMAL = 0.85 # 通常時の閾値 (85点)
SIGNAL_THRESHOLD_ACTIVE = 0.83 # リスクオン時の緩めの閾値 (83点)
FGI_PROXY_BONUS_MAX = 0.5 # FGIによる最大影響度 (±50%)

# FGI proxy ( -1.0 to 1.0 ) による取引環境の判定閾値
FGI_ACTIVE_THRESHOLD = 0.05  # FGI proxyがこれ以上の場合はリスクオンと見なす
FGI_SLUMP_THRESHOLD = -0.05  # FGI proxyがこれ以下の場合はリスクオフと見なす


# ====================================================================================
# GLOBAL VARIABLES (状態管理用)
# ====================================================================================

EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # 1時間ごとの通知時刻

# 修正箇所: long_term_trend_bonus を追加
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0, 'long_term_trend_bonus': 0.0} 
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
LAST_MACRO_UPDATE_TIME: float = 0.0 # マクロ環境データの最終更新時刻
MACRO_UPDATE_INTERVAL = 60 * 30 # マクロデータの更新間隔 (30分)


# ====================================================================================
# UTILITIES for MACRO ANALYSIS (New)
# ====================================================================================

def calculate_long_term_trend_bonus(df: pd.DataFrame) -> float:
    """
    all_data.csvの日次データに基づいて長期トレンドボーナスを計算する。
    - ボーナスは -0.02 (リスクオフ) から +0.04 (強力なリスクオン) の範囲で計算される。
    """
    if df.empty or len(df) < 200:
        logging.warning("⚠️ all_data.csvのデータが不足しているため、マクロトレンドボーナスは0.0です。")
        return 0.0

    # 長期移動平均線 (SMA50, SMA200) の計算
    # 既存のインデックスがDatetimeIndexであることを前提とする
    df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
    df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')

    df['SMA50'] = ta.sma(df['Close'], length=50)
    df['SMA200'] = ta.sma(df['Close'], length=200)
    df = df.dropna().reset_index(drop=True)
    
    if df.empty:
        return 0.0
        
    last_close = df['Close'].iloc[-1]
    last_sma50 = df['SMA50'].iloc[-1]
    last_sma200 = df['SMA200'].iloc[-1]
    
    bonus = 0.0
    
    # 1. 強力な強気トレンド: SMA50 > SMA200 (ゴールデンクロス状態)
    if last_sma50 > last_sma200:
        bonus += 0.02 # 2点ボーナス
        
    # 2. 短期強気: 現在価格 > SMA50
    if last_close > last_sma50:
        bonus += 0.01 # 1点ボーナス
        
    # 3. 出来高確認: 最新の出来高が過去200日の平均出来高より1.2倍以上
    avg_volume = df['Volume'].iloc[-200:].mean()
    last_volume = df['Volume'].iloc[-1]
    if last_volume > avg_volume * 1.2:
        bonus += 0.01 # 1点ボーナス (出来高が伴うトレンド)

    # 4. SMA50 < SMA200 (デッドクロス状態) の場合、ペナルティ
    if last_sma50 < last_sma200:
        bonus -= 0.02 # 2点ペナルティ (リスクオフ)

    # ボーナスの最大/最小をクリップ (-0.02から+0.04)
    return max(-0.02, min(0.04, bonus))

async def fetch_all_data_csv() -> Optional[pd.DataFrame]:
    """添付ファイル all_data.csv を読み込む"""
    try:
        # ファイルパスを取得 (この環境では直接ファイル名を使用)
        file_path = "all_data.csv" 
        
        # 非同期処理としてファイルの読み込みを実行
        df = await asyncio.to_thread(
            pd.read_csv,
            file_path,
            index_col='Date',
            parse_dates=True
        )
        
        # 必要な列 'Close' と 'Volume' が存在するか確認
        if 'Close' not in df.columns or 'Volume' not in df.columns:
            logging.error("❌ all_data.csv に 'Close' または 'Volume' 列が見つかりません。")
            return None
            
        logging.info(f"✅ all_data.csv の読み込み成功。データ数: {len(df)}")
        return df
        
    except FileNotFoundError:
        logging.error("❌ all_data.csv が見つかりません。ファイルパスを確認してください。")
        return None
    except Exception as e:
        logging.error(f"❌ all_data.csv の読み込み中にエラーが発生: {e}")
        return None


# ====================================================================================
# UTILITIES (汎用関数)
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントの初期化と接続テストを行う"""
    global EXCHANGE_CLIENT
    
    # クライアントが既に初期化されている場合はスキップ
    if EXCHANGE_CLIENT:
        return

    try:
        logging.info(f"⏳ {EXCHANGE_ID} クライアントを初期化しています...")
        
        # CCXTクライアントの動的生成
        exchange_class = getattr(ccxt_async, EXCHANGE_ID)
        
        # クライアントインスタンスの作成
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET,
            'enableRateLimit': True, # レート制限対策を有効化
            'options': {
                'defaultType': 'future', # デフォルトを先物市場に設定
                # ===========================================================
                # 🔥 v19.0.55 HOTFIX: Bybitのジオブロック回避のための設定
                # CCXTに、スポットとオプション市場の読み込みをスキップするよう指示
                'loadMarkets': { 
                    'spot': False,
                    'swap': True, 
                    'option': False, 
                    'future': True
                },
                # ===========================================================
            }
        })
        
        # 接続テスト (load_markets)
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ {EXCHANGE_ID} への接続に成功しました。BOTバージョン: {BOT_VERSION}")

    except Exception as e:
        logging.critical(f"❌ {EXCHANGE_ID} の初期化に失敗しました。BOTを終了します。エラー: {e}")
        # 致命的なエラーのため、FastAPIをシャットダウン
        sys.exit(1)


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """pandas-taを使用してATRを計算し、最新のATR値を返す"""
    if len(df) < period:
        # ログに警告を出力し、デフォルト値（例: 0.0001 = $10000のBTCで$10変動）を返す
        logging.warning(f"⚠️ ATR計算のためのデータが不足しています (必要:{period} vs 現在:{len(df)})")
        # 価格に基づいて安全なデフォルト値を設定する
        if not df.empty:
            return df['Close'].iloc[-1] * 0.0001
        return 0.1 # 完全にデータがない場合は最低値を返す

    atr_series = ta.atr(df['High'], df['Low'], df['Close'], length=period)
    # 最新のATR値を返す
    return atr_series.iloc[-1] if not atr_series.empty else 0.1


def get_current_threshold(macro_context: Dict) -> float:
    """FGI proxyと長期トレンドボーナスに基づいて現在の取引閾値を動的に決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    long_term_trend_bonus = macro_context.get('long_term_trend_bonus', 0.0) # NEW
    
    # 総合マクロ環境スコアの計算（FGIと長期トレンドを合算）
    # FGIは-1.0 to 1.0, Bonusは-0.02 to 0.04
    total_macro_score = fgi_proxy + long_term_trend_bonus 
    
    # 評価: 強いリスクオン/リスクオフの判定
    if total_macro_score > FGI_ACTIVE_THRESHOLD + 0.05: # より強いリスクオン (+0.1程度)
        return SIGNAL_THRESHOLD_ACTIVE
    elif total_macro_score < FGI_SLUMP_THRESHOLD - 0.05: # より強いリスクオフ (-0.1程度)
        return SIGNAL_THRESHOLD_SLUMP
    else:
        # FGIと長期トレンドの重み付き平均を使用して、SIGNAL_THRESHOLD_NORMALをベースに調整する
        
        # total_macro_score を -0.1 から 0.1 の範囲でクリップ
        # この範囲が取引閾値の調整に影響を与える
        normalized_score = max(-0.1, min(0.1, total_macro_score))
        
        # 正規化スコアに応じて、NORMAL閾値から調整
        # 調整率 (例: 0.1変化で約 0.03 変化)
        adjustment = normalized_score * 0.3 
        
        # リスクオン (高スコア) の場合は閾値を下げる (より取引しやすく)
        # リスクオフ (低スコア) の場合は閾値を上げる (より厳しく)
        return max(SIGNAL_THRESHOLD_ACTIVE, min(SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL - adjustment))


def format_startup_message(
    current_threshold: float,
    macro_context: Dict,
    balance: Dict[str, float]
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    long_term_trend_bonus = macro_context.get('long_term_trend_bonus', 0.0) # NEW
    
    # マクロ環境のテキスト判定
    if fgi_proxy > FGI_ACTIVE_THRESHOLD:
        market_condition_text = "リスクオン (積極的な取引)"
    elif fgi_proxy < FGI_SLUMP_THRESHOLD:
        market_condition_text = "リスクオフ (慎重な取引)"
    else:
        market_condition_text = "中立"
        
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🚀 <b>Apex BOT v{BOT_VERSION} - 起動完了</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"⏰ 最終更新時刻: <code>{now_jst}</code>\n"
        f"🤖 取引ステータス: <b>{trade_status}</b>\n"
        f"⚙️ 対象シンボル: <code>{', '.join(CURRENT_MONITOR_SYMBOLS)}</code>\n"
        f"💼 1回あたりの取引サイズ: <code>{POSITION_SIZE_USDT:.2f} USDT</code>\n\n"
    )

    balance_section = (
        f"💰 <b>アカウント情報</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **残高 (USDT)**: <code>{balance.get('free', 0.0):.2f}</code> (利用可能)\n"
        f"  - **合計残高 (USDT)**: <code>{balance.get('total', 0.0):.2f}</code>\n\n"
    )

    macro_influence_score = (
        fgi_proxy * FGI_PROXY_BONUS_MAX + 
        forex_bonus * FGI_PROXY_BONUS_MAX + 
        long_term_trend_bonus
    ) * 100

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code>\n"
        # 修正箇所: long_term_trend_bonus を追加して表示
        f"  - **長期トレンドボーナス**: <code>{long_term_trend_bonus * 100:.2f}</code> 点\n"
        f"  - **総合マクロ影響**: <code>{macro_influence_score:.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"💡 {EXCHANGE_ID} にて自動監視を開始します。\n"
    )

    return header + balance_section + macro_section + footer


async def send_telegram_message(message: str):
    """Telegramにメッセージを送信する"""
    if not NOTIFICATION_ENABLED:
        return
        
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            'chat_id': TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML' # HTMLタグの使用を許可
        }
        # 非同期でリクエストを送信
        await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        
    except Exception as e:
        logging.error(f"❌ Telegramへの通知送信中にエラーが発生: {e}")


async def send_discord_message(message: str):
    """Discordへのメッセージ送信ロジック (実装が必要であれば追加)"""
    pass

async def send_notification(message: str):
    """統合された通知関数"""
    # 実際はTelegram以外にもDiscordやSlackなどの通知ロジックを追加可能
    await send_telegram_message(message)


async def get_account_balance(symbol: str = 'USDT') -> Dict[str, float]:
    """アカウント残高を取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return {'total': 0.0, 'free': 0.0, 'used': 0.0}
        
    try:
        # fetch_balanceは全残高を取得するため、引数は不要だが、USDT建て残高に焦点を当てる
        balance_data = await EXCHANGE_CLIENT.fetch_balance()
        
        # USDT残高を抽出
        if symbol in balance_data:
            total = balance_data[symbol].get('total', 0.0)
            free = balance_data[symbol].get('free', 0.0)
            used = balance_data[symbol].get('used', 0.0)
            return {'total': total, 'free': free, 'used': used}
            
        return {'total': 0.0, 'free': 0.0, 'used': 0.0}

    except Exception as e:
        logging.error(f"❌ 残高の取得に失敗: {e}")
        return {'total': 0.0, 'free': 0.0, 'used': 0.0}

async def get_open_positions(symbol: str) -> List[Dict]:
    """オープンポジションを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return []
        
    try:
        # fetch_positionsを呼び出し
        positions = await EXCHANGE_CLIENT.fetch_positions([symbol])
        
        # ccxtの仕様に基づき、空でないポジションのみをフィルタリング
        open_positions = [
            p for p in positions 
            if p.get('contracts', 0) != 0 and p.get('side') in ['long', 'short']
        ]

        return open_positions

    except ccxt.ExchangeError as e:
        # ポジションがない場合や、APIエラーの場合
        logging.warning(f"⚠️ {symbol}のオープンポジション取得でExchangeError: {e}")
        return []
    except Exception as e:
        logging.error(f"❌ オープンポジションの取得に失敗: {e}")
        return []


async def cancel_all_orders(symbol: str):
    """指定されたシンボルのオープン注文をすべてキャンセルする"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return
        
    try:
        await EXCHANGE_CLIENT.cancel_all_orders(symbol)
        logging.info(f"✅ {symbol} のオープン注文をすべてキャンセルしました。")
    except Exception as e:
        logging.warning(f"⚠️ {symbol} の注文キャンセルに失敗: {e}")


async def create_oco_orders(
    symbol: str, 
    side: str, 
    amount: float, 
    entry_price: float, 
    sl_price: float, 
    tp_price: float
) -> Tuple[Optional[str], Optional[str]]:
    """
    ポジションに対してSLとTPの注文をOCO（One-Cancels-the-Other）的に設定する。
    TPはLIMIT、SLはSTOP_MARKETとして設定する。
    """
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None, None
        
    try:
        # SL注文
        params_sl = {
            'stopLossPrice': sl_price,
            'triggerPrice': sl_price, # Bybitなどで使用されるトリガー価格
            # 'closeOnTrigger': True, # クロスポジションを閉じる設定（取引所による）
        }
        
        # TP注文
        params_tp = {
            'takeProfitPrice': tp_price,
            'triggerPrice': tp_price, # Bybitなどで使用されるトリガー価格
            # 'closeOnTrigger': True,
        }
        
        # OCO注文を表現するために、ポジション決済注文を個別に発行することが多い
        # TP注文 (LIMIT)
        tp_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='LIMIT',
            side=side, # 決済サイド (e.g., LongならShort, ShortならLong)
            amount=amount,
            price=tp_price,
            params=params_tp # TP価格をパラメーターとして渡す（取引所が対応している場合）
        )
        tp_id = tp_order.get('id')
        logging.info(f"✅ {symbol} TP注文 (LIMIT {tp_price:.4f}) を発注しました。ID: {tp_id}")
        
        # SL注文 (STOP_MARKET)
        sl_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='STOP_MARKET', # STOP_MARKETはSTOP_LOSS_LIMITよりも確実な決済
            side=side, # 決済サイド
            amount=amount,
            price=None, # 成行なので価格は指定しない
            params=params_sl
        )
        sl_id = sl_order.get('id')
        logging.info(f"✅ {symbol} SL注文 (STOP_MARKET {sl_price:.4f}) を発注しました。ID: {sl_id}")

        return sl_id, tp_id

    except Exception as e:
        logging.error(f"❌ {symbol} のSL/TP注文の発注に失敗: {e}")
        return None, None

async def execute_trade(symbol: str, side: str, amount_usdt: float, current_price: float) -> Optional[Dict]:
    """
    実際の取引執行（MARKET注文）とSL/TP注文の設定を行う
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        logging.error("❌ Exchangeクライアントが初期化されていません。")
        return None

    if TEST_MODE:
        logging.info(f"🧪 TEST MODE: {symbol}で{side} ({amount_usdt:.2f} USDT) の取引をスキップしました。")
        return {
            'id': f'TEST-{uuid.uuid4()}',
            'symbol': symbol,
            'side': side,
            'amount': amount_usdt,
            'price': current_price
        }

    try:
        # レバレッジ設定
        await EXCHANGE_CLIENT.set_leverage(LEVERAGE, symbol)
        
        # シンプルにベース通貨での取引量を計算 (レバレッジ適用前のサイズを現在の価格で割る)
        amount_in_base = amount_usdt / current_price 
        
        # マーケット注文
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='MARKET',
            side=side,
            amount=amount_in_base,
            params={'leverage': LEVERAGE} # レバレッジを再度指定
        )
        
        entry_price = float(order.get('price') or current_price)
        logging.info(f"✅ {symbol} {side.upper()} @ {entry_price:.4f} でエントリーに成功しました。")

        # ATRを計算し、SL/TP価格を決定
        ohlcv_data = await fetch_ohlcv_data(symbol, timeframe='1h') # 1hのデータでATRを計算
        if ohlcv_data.empty:
            logging.warning("⚠️ ATR計算用のOHLCVデータが取得できませんでした。SL/TP設定をスキップします。")
            return order

        atr = calculate_atr(ohlcv_data, ATR_PERIOD)
        atr_value = atr * entry_price # USDTでの価格変動幅

        sl_price, tp_price = 0.0, 0.0

        if side == 'buy':
            sl_price = entry_price - atr_value * ATR_MULTIPLIER_SL
            tp_price = entry_price + atr_value * ATR_MULTIPLIER_TP
            # 決済サイドは 'sell'
            close_side = 'sell'
        else: # sell (short)
            sl_price = entry_price + atr_value * ATR_MULTIPLIER_SL
            tp_price = entry_price - atr_value * ATR_MULTIPLIER_TP
            # 決済サイドは 'buy'
            close_side = 'buy'
            
        # OCO注文（SL/TP）の発注
        # amount_in_base はポジションサイズと一致
        await create_oco_orders(
            symbol=symbol,
            side=close_side,
            amount=amount_in_base, 
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price
        )
        
        return order

    except ccxt.ExchangeError as e:
        # 取引所のAPIエラー（例：残高不足、レート制限、IOC注文の失敗）
        # IOC (Immediate Or Cancel) 注文失敗時の診断ログを追加
        if 'ImmediateOrCancel' in str(e) or 'IOC' in str(e):
             logging.error(f"❌ {symbol} エントリー失敗 (ExchangeError: IOC失敗の可能性): {e}")
        else:
             logging.error(f"❌ {symbol} エントリー失敗 (ExchangeError): {e}")
        return None
    except Exception as e:
        logging.error(f"❌ {symbol} エントリー中に予期せぬエラーが発生: {e}")
        return None

# ====================================================================================
# DATA & ANALYSIS
# ====================================================================================

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """OHLCVデータを取得し、DataFrameとして返す"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return pd.DataFrame()
        
    try:
        # ohlcvデータを取得
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit
        )
        
        # DataFrameに変換
        df = pd.DataFrame(
            ohlcv,
            columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume']
        )
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('Timestamp', inplace=True)
        return df

    except Exception as e:
        logging.error(f"❌ {symbol} {timeframe}のOHLCVデータ取得に失敗: {e}")
        return pd.DataFrame()


def analyze_indicator(df: pd.DataFrame, timeframe: str) -> Tuple[int, Optional[str]]:
    """
    RSIとMACDに基づいて取引シグナルをスコアリングする
    - スコア (0-100) とシグナル ('buy', 'sell', 'none') を返す
    """
    if df.empty or len(df) < MACD_SLOW + MACD_SIGNAL: # MACD計算に必要な最低限の期間
        return 0, 'none'
        
    # 1. RSIの計算とスコアリング
    df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)
    last_rsi = df['RSI'].iloc[-1]
    
    rsi_score = 0
    rsi_signal = 'none'
    
    # RSIが売られすぎ（30以下）: 買いシグナル
    if last_rsi <= RSI_OVERSOLD:
        rsi_score = RSI_SCORE_WEIGHT
        rsi_signal = 'buy'
    # RSIが買われすぎ（70以上）: 売りシグナル
    elif last_rsi >= RSI_OVERBOUGHT:
        rsi_score = RSI_SCORE_WEIGHT
        rsi_signal = 'sell'

    # 2. MACDの計算とスコアリング
    macd_data = ta.macd(
        df['Close'], 
        fast=MACD_FAST, 
        slow=MACD_SLOW, 
        signal=MACD_SIGNAL, 
        append=True
    )
    # MACDデータがDataFrameで返されることを確認
    if macd_data.empty:
        logging.warning("⚠️ MACDデータが空です。スコア計算をスキップします。")
        return 0, 'none'

    # カラム名が 'MACD_{fast}_{slow}_{signal}' などとなるため、正規表現で取得
    macd_col = [col for col in macd_data.columns if 'MACD_' in col][-1]
    hist_col = [col for col in macd_data.columns if 'HIST_' in col][-1]
    
    last_macd = macd_data[macd_col].iloc[-1]
    last_hist = macd_data[hist_col].iloc[-1]

    macd_score = 0
    macd_signal = 'none'
    
    # MACDがシグナルラインを上回る (MACD > Signal) & MACDヒストグラムが上昇
    # MACD > 0 かつ ヒストグラムが正 or ヒストグラムがゼロから上向き
    if last_macd > 0 and last_hist > 0:
        macd_score = MACD_SCORE_WEIGHT
        macd_signal = 'buy'
    # MACDがシグナルラインを下回る (MACD < Signal) & MACDヒストグラムが下降
    elif last_macd < 0 and last_hist < 0:
        macd_score = MACD_SCORE_WEIGHT
        macd_signal = 'sell'
        
    # 3. 総合スコアとシグナルの決定
    total_score = 0
    final_signal = 'none'
    
    # シグナルが一致している場合のみスコアを合算
    if rsi_signal == 'buy' and macd_signal == 'buy':
        total_score = rsi_score + macd_score
        final_signal = 'buy'
    elif rsi_signal == 'sell' and macd_signal == 'sell':
        total_score = rsi_score + macd_score
        final_signal = 'sell'
    else:
        # シグナルが不一致の場合は、スコアをRSIまたはMACD単独の最高スコアに制限
        # これは、片方のみのシグナルでは確度が低いと見なすため
        total_score = max(rsi_score, macd_score) 
        final_signal = 'none' # 不一致の場合は取引しない

    return int(total_score), final_signal


async def fetch_macro_context() -> Dict: 
    """
    Crypto Fear & Greed Index (FGI) と all_data.csv からのマクロ環境コンテキストを返す。
    FGIは-1.0 (Extreme Fear) から 1.0 (Extreme Greed) に正規化し、proxy値として返す。
    """
    global LAST_MACRO_UPDATE_TIME, GLOBAL_MACRO_CONTEXT
    now = time.time()
    
    # 更新間隔内の場合はキャッシュを返す
    if now - LAST_MACRO_UPDATE_TIME < MACRO_UPDATE_INTERVAL:
        logging.info("📊 マクロコンテキストはキャッシュを使用します。")
        return GLOBAL_MACRO_CONTEXT

    # 1. Fear & Greed Index (FGI) の取得
    fgi_proxy = 0.0 # -1.0から1.0
    fgi_raw_value = 'N/A'
    
    fgi_url = "https://api.alternative.me/fng/?limit=1"
    try:
        response = await asyncio.to_thread(requests.get, fgi_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            index_value = int(data['data'][0]['value'])
            fgi_raw_value = f"{index_value} ({data['data'][0]['value_classification']})"
            # FGI (0-100) を -1.0から1.0に正規化 (0 -> -1.0, 50 -> 0.0, 100 -> 1.0)
            fgi_proxy = (index_value - 50) / 50.0
            
    except Exception as e:
        logging.warning(f"⚠️ FGIデータ取得失敗: {e}。デフォルト値を使用します。")
        
    # 2. Forex Market Dataの取得 (BTC/USDやドルインデックスなどがあればここで取得しボーナスに変換)
    forex_bonus = 0.0 # 暫定的に0.0を返す
    
    # 3. all_data.csvからの長期トレンドボーナス計算 (NEW)
    long_term_trend_bonus = 0.0
    df_macro = await fetch_all_data_csv()
    if df_macro is not None:
        long_term_trend_bonus = calculate_long_term_trend_bonus(df_macro)
        logging.info(f"📊 長期トレンドボーナス (all_data.csv): {long_term_trend_bonus*100:.2f} 点")
        
    logging.info(f"📊 FGI: {fgi_raw_value} (Proxy: {fgi_proxy:.2f})")
    
    LAST_MACRO_UPDATE_TIME = now
    
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus,
        'long_term_trend_bonus': long_term_trend_bonus # NEW
    }

# ====================================================================================
# MAIN BOT LOGIC
# ====================================================================================

async def open_order_management_loop():
    """オープンポジションと未決済のSL/TP注文を監視し、必要に応じて再設定する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        logging.error("❌ クライアントが未初期化です。注文監視をスキップします。")
        return
        
    for symbol in CURRENT_MONITOR_SYMBOLS:
        try:
            # 1. オープンポジションの取得
            positions = await get_open_positions(symbol)
            if not positions:
                # ポジションがない場合は次へ
                continue
                
            # 2. ポジションと注文の情報を整理
            position = positions[0]
            position_side = position['side']
            position_amount = abs(position['contracts']) # ポジションの絶対量
            entry_price = position['entryPrice'] or position['info'].get('avgPrice')
            
            # 3. オープン注文の取得
            open_orders = await EXCHANGE_CLIENT.fetch_open_orders(symbol)
            
            # SL/TP注文の有無を確認
            has_sl = any('STOP' in order['type'].upper() for order in open_orders)
            has_tp = any('LIMIT' in order['type'].upper() and order['side'] != position_side for order in open_orders)

            # 4. SL/TP再設定ロジック
            if RE_ADJUST_SL_TP_ENABLED and (not has_sl or not has_tp):
                logging.warning(f"⚠️ {symbol} ポジション ({position_side}) のSL/TP注文が不足しています (SL:{has_sl}, TP:{has_tp})。再設定します。")
                
                # b. ATRに基づいて新しいSL/TP価格を計算
                ohlcv_data = await fetch_ohlcv_data(symbol, timeframe='1h') # 1hのデータでATRを計算
                if ohlcv_data.empty or not entry_price:
                    logging.error(f"❌ {symbol} ATR計算データ不足、またはエントリー価格不明。SL/TP再設定を中断。")
                    continue
                    
                atr = calculate_atr(ohlcv_data, ATR_PERIOD)
                atr_value = atr * entry_price # USDTでの価格変動幅

                new_sl_price, new_tp_price = 0.0, 0.0

                if position_side == 'long':
                    new_sl_price = entry_price - atr_value * ATR_MULTIPLIER_SL
                    new_tp_price = entry_price + atr_value * ATR_MULTIPLIER_TP
                    close_side = 'sell'
                else: # short
                    new_sl_price = entry_price + atr_value * ATR_MULTIPLIER_SL
                    new_tp_price = entry_price - atr_value * ATR_MULTIPLIER_TP
                    close_side = 'buy'
                    
                # c. SL/TP再発注
                # まず、現在の未決済のSL/TP注文をすべてキャンセル
                orders_to_cancel = [
                    order for order in open_orders 
                    if 'STOP' in order['type'].upper() or ('LIMIT' in order['type'].upper() and order['side'] == close_side)
                ]
                for order in orders_to_cancel:
                    try:
                        await EXCHANGE_CLIENT.cancel_order(order['id'], symbol)
                        logging.info(f"   ↳ 既存の注文 (ID:{order['id']}) をキャンセルしました。")
                    except Exception as ce:
                        logging.warning(f"   ↳ 注文キャンセル失敗 (ID:{order['id']}): {ce}")


                await create_oco_orders(
                    symbol=symbol,
                    side=close_side,
                    amount=position_amount, 
                    entry_price=entry_price,
                    sl_price=new_sl_price,
                    tp_price=new_tp_price
                )
                
                logging.info(f"✅ {symbol} SL/TP注文を再設定しました (SL:{new_sl_price:.4f}, TP:{new_tp_price:.4f})。")

        except Exception as e:
            logging.error(f"❌ {symbol} の注文監視処理中にエラーが発生: {e}")


async def main_bot_loop():
    """BOTのメイン処理ループ"""
    # 修正: すべての global 変数を関数の冒頭で宣言
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT, LAST_HOURLY_NOTIFICATION_TIME
    now_ts = time.time()
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    logging.info(f"--- 💡 {now_jst} - BOT LOOP START (M1 Frequency) ---")

    # 1. FGIデータを取得し、グローバルマクロコンテキストを更新
    GLOBAL_MACRO_CONTEXT = await fetch_macro_context() 
    
    # マクロ影響スコアの計算に long_term_trend_bonus を追加
    macro_influence_score = (
        GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX + 
        GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0) * FGI_PROXY_BONUS_MAX + 
        GLOBAL_MACRO_CONTEXT.get('long_term_trend_bonus', 0.0)
    ) * 100
    
    # 動的取引閾値の取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    logging.info(f"📊 動的取引閾値: {current_threshold*100:.2f} / 100 (マクロ影響: {macro_influence_score:.2f} 点)")
    
    # 2. 1時間ごとの通知 (初回起動時を除く)
    if IS_FIRST_MAIN_LOOP_COMPLETED and now_ts - LAST_HOURLY_NOTIFICATION_TIME > 3600:
        balance = await get_account_balance('USDT')
        message = format_startup_message(current_threshold, GLOBAL_MACRO_CONTEXT, balance)
        await send_notification(message)
        # 修正済み: global宣言は上部で行われているため、ここでは代入のみ
        LAST_HOURLY_NOTIFICATION_TIME = now_ts
        logging.info("🔔 定期通知を送信しました。")


    # 3. 各シンボルに対する分析と取引の実行
    latest_signals = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        try:
            # a. 既にポジションを持っているか確認
            open_positions = await get_open_positions(symbol)
            if open_positions:
                # ポジションを持っている場合はスキップ
                logging.info(f"👉 {symbol}: ポジションがあるため、新たなシグナル分析をスキップします。")
                latest_signals.append({
                    'symbol': symbol,
                    'timeframe': '1h',
                    'score': -1,
                    'signal': 'HOLD',
                    'position': open_positions[0]['side'].upper()
                })
                continue
                
            # b. OHLCVデータの取得（ここでは1時間足を使用）
            timeframe = '1h'
            df = await fetch_ohlcv_data(symbol, timeframe)
            if df.empty:
                continue

            # c. テクニカル分析
            total_score, final_signal = analyze_indicator(df, timeframe)
            
            latest_signals.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'score': total_score,
                'signal': final_signal.upper(),
                'position': 'NONE'
            })
            
            logging.info(f"🔍 {symbol} ({timeframe}): スコア {total_score} / 100, シグナル: {final_signal.upper()}")
            
            # d. 取引シグナル判定
            if final_signal != 'none' and total_score >= current_threshold * 100:
                # 閾値を超えた強力なシグナル
                
                # 前回の取引から最低間隔が経過しているかチェック
                last_signal_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
                if now_ts - last_signal_time < LOOP_INTERVAL * 2: # 2周期待つ
                    logging.info(f"⏳ {symbol} : シグナル発生。しかし、前回のシグナル ({LOOP_INTERVAL*2}秒以内) のためスキップ。")
                    continue

                # ⚠️ ここで、既存のオープン注文をキャンセル (SL/TPではない他の注文がある場合に備えて)
                await cancel_all_orders(symbol)
                
                # 現在価格の取得（OHLCVの最新終値を使用）
                current_price = df['Close'].iloc[-1]
                
                # 取引の実行
                trade_result = await execute_trade(
                    symbol=symbol,
                    side=final_signal, # 'buy' or 'sell'
                    amount_usdt=POSITION_SIZE_USDT,
                    current_price=current_price
                )
                
                if trade_result:
                    LAST_SIGNAL_TIME[symbol] = now_ts # 成功した場合のみ最終シグナル時刻を更新
                    
                    # 取引通知メッセージの作成
                    side_text = '🚀 LONG (買い)' if final_signal == 'buy' else '🐻 SHORT (売り)'
                    message = (
                        f"🔥 <b>トレード実行通知</b> 🔥\n"
                        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                        f"  - **シンボル**: <code>{symbol}</code>\n"
                        f"  - **サイド**: <b>{side_text}</b>\n"
                        f"  - **取引量**: <code>{POSITION_SIZE_USDT:.2f} USDT</code>\n"
                        f"  - **スコア**: <code>{total_score} / 100</code> (閾値:{current_threshold*100:.0f})\n"
                        f"  - **エントリー価格**: <code>{current_price:.4f}</code>\n"
                        f"  - **時間足**: <code>{timeframe}</code>\n"
                        f"  - **マクロ影響**: <code>{macro_influence_score:.2f}</code> 点\n"
                    )
                    await send_notification(message)


        except Exception as e:
            logging.error(f"❌ {symbol} のメイン処理中に予期せぬエラーが発生: {e}")
            
    # 4. ループ後の処理
    LAST_ANALYSIS_SIGNALS = latest_signals # 最新の分析結果を保存
    LAST_SUCCESS_TIME = now_ts
    IS_FIRST_MAIN_LOOP_COMPLETED = True
    logging.info(f"--- 🟢 {now_jst} - BOT LOOP END ---")


# ====================================================================================
# SCHEDULER & ENTRY POINT
# ====================================================================================

async def bot_main_scheduler():
    """メインBOTループを定期実行するスケジューラ"""
    # 初回起動時は即座に実行
    await asyncio.sleep(1) 
    
    while True:
        try:
            await main_bot_loop()
            
            # 初回起動完了通知 (一度だけ)
            # 修正: global宣言は上部で行う
            global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_HOURLY_NOTIFICATION_TIME
            
            if IS_FIRST_MAIN_LOOP_COMPLETED and LAST_HOURLY_NOTIFICATION_TIME == 0.0:
                balance = await get_account_balance('USDT')
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                message = format_startup_message(current_threshold, GLOBAL_MACRO_CONTEXT, balance)
                await send_notification(message)
                
                LAST_HOURLY_NOTIFICATION_TIME = time.time() # 初回通知時刻を記録
                
        except Exception as e:
            logging.critical(f"❌ 致命的なエラーによりメインループが中断: {e}")
            
            # 致命的エラー発生時の通知
            try:
                error_message = (
                    f"🚨 <b>【致命的エラー発生】 BOT中断</b> 🚨\n"
                    f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                    f"  - **エラー内容**: <code>{type(e).__name__}: {e}</code>\n"
                    f"  - **BOTバージョン**: <code>{BOT_VERSION}</code>\n"
                    f"  - **次回試行**: <code>{LOOP_INTERVAL}秒後</code>\n"
                    f"  - **推奨**: **手動でポジションを確認してください**"
                )
                await send_notification(error_message)
            except Exception as notify_e:
                 logging.error(f"❌ 致命的エラー通知の送信に失敗: {notify_e}")

        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


async def open_order_management_scheduler():
    """オープン注文監視ループを定期実行するスケジューラ (10秒ごと)"""
    # 初回起動後の待機時間を考慮し、初回は少し遅延させて実行
    await asyncio.sleep(15)
    
    while True:
        try:
            # メインループに影響を与えないように、監視は別タスクで実行
            await open_order_management_loop() 
        except Exception as e:
            # 注文監視のエラーは致命的ではないことが多いが、ログに記録
            logging.error(f"❌ 注文監視ループ実行中にエラーが発生: {e}")
            
        # 次のループまで待機
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI & ENTRY POINT
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインのタスクを開始する"""
    logging.info("🚀 BOTの起動処理を開始します...")
    
    # CCXTクライアントの初期化
    await initialize_exchange_client()
    
    # メインBOTのタスクを開始
    asyncio.create_task(bot_main_scheduler())
    
    # 注文監視タスクを開始
    asyncio.create_task(open_order_management_scheduler())
    
    logging.info("✅ BOTのメインタスクと監視タスクが開始されました。")


@app.get("/")
async def root():
    """BOTのステータス情報を提供するエンドポイント"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 最新の残高を取得
    balance = await get_account_balance('USDT')
    
    # 最新のポジション情報を取得
    position_info = {}
    for symbol in CURRENT_MONITOR_SYMBOLS:
        positions = await get_open_positions(symbol)
        position_info[symbol] = [
            {
                'side': p.get('side'),
                'amount': p.get('contracts'),
                'entry_price': p.get('entryPrice') or p['info'].get('avgPrice'),
                'leverage': p.get('leverage')
            } for p in positions
        ]

    # マクロ影響スコアの計算
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    forex_bonus = GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0)
    long_term_trend_bonus = GLOBAL_MACRO_CONTEXT.get('long_term_trend_bonus', 0.0)
    macro_influence_score = (
        fgi_proxy * FGI_PROXY_BONUS_MAX + 
        forex_bonus * FGI_PROXY_BONUS_MAX + 
        long_term_trend_bonus
    ) * 100
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    return JSONResponse(content={
        "status": "RUNNING" if IS_FIRST_MAIN_LOOP_COMPLETED else "INITIALIZING",
        "bot_version": BOT_VERSION,
        "last_update": now_jst,
        "exchange_id": EXCHANGE_ID,
        "test_mode": TEST_MODE,
        "macro_context": {
            "fgi_raw": GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A'),
            "fgi_proxy": fgi_proxy,
            "forex_bonus": forex_bonus,
            "long_term_trend_bonus": long_term_trend_bonus,
            "macro_influence_score": f'{macro_influence_score:.2f} / 100',
            "current_threshold_score": f'{current_threshold * 100:.2f} / 100'
        },
        "account_balance_usdt": balance,
        "open_positions": position_info,
        "last_analysis_signals": LAST_ANALYSIS_SIGNALS
    })


if __name__ == "__main__":
    # 開発環境で直接実行する場合 (デプロイ環境では不要)
    # PORT=8080 が環境変数で設定されていない場合はデフォルトの 8000 を使用
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get('PORT', 8000)))
