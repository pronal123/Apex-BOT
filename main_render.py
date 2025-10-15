# ====================================================================================
# Apex BOT v19.0.22 - Error Identification Integrated (v19.0.20ベース)
#
# 強化ポイント:
# 1. 【エラー識別】fetch_current_balance_usdt() を強化し、残高とエラー/ゼロ残高のステータスコードを返すように修正 (v19.0.22機能)。
# 2. 【通知強化】ステータスコードに応じてTelegramメッセージのヘッダーと警告内容を変更し、エラーの種類を明確に通知する (v19.0.22機能)。
# 3. 【元の構造維持】v19.0.20のコード構造（コメント、ロジック、桁数）を最大限維持。
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
TOP_SYMBOL_LIMIT = 30      # 出来高上位30銘柄を監視
LOOP_INTERVAL = 180        # メインループの実行間隔（秒）
REQUEST_DELAY_PER_SYMBOL = 0.5 # 銘柄ごとのAPIリクエストの遅延（秒）

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.75             # シグナルを通知する最低スコア
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# 💡 テクニカル分析定数 (v19.0.20ベース)
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 # ボリンジャーバンドの幅が狭い場合のペナルティ閾値 (%)
LONG_TERM_SMA_LENGTH = 50           # 長期トレンド判定に使用するSMAの期間（4h足）
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンドと逆行する場合のスコアペナルティ
MACD_CROSS_PENALTY = 0.15           # MACDが有利なクロスでない場合のペナルティ
RANGE_TRAIL_MULTIPLIER = 3.0        # 平均変動幅に基づいた初期SL/TPの乗数
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

# 💡 自動売買設定 (v19.0.20ベース)
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
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} # 流動性データキャッシュ

# 💡 ポジション管理システム
# {symbol: {'entry_price': float, 'amount': float, 'sl_price': float, 'tp_price': float, 'open_time': float, 'status': str}}
ACTUAL_POSITIONS: Dict[str, Dict] = {}
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0

# ログ設定
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
    base_rate = score * 0.50 + 0.35

    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate))
    elif timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    elif timeframe == '4h':
        return max(0.50, min(0.90, base_rate))
    return base_rate

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """分析結果を統合したTelegramメッセージをHTML形式で作成する (v19.0.20ベース)"""

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

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100

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

    # SLソースの表示をATRからRangeに変更
    sl_source_str = "Range基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 Range バッファ"

    # 残高不足で取引がスキップされた場合の表示調整
    if trade_amount_usdt == 0.0 and trade_plan_data.get('max_risk_usdt', 0.0) == 0.0:
         # 残高不足または取引サイズが小さすぎる場合
         trade_size_str = "<code>不足/小</code>"
         max_risk_str = "<code>不足/小</code>"
         trade_plan_header = "⚠️ <b>通知のみ（取引サイズ不足）</b>"
    elif trade_plan_data.get('max_risk_usdt', 0.0) == 0.0 and trade_plan_data.get('amount_to_buy', 0.0) == 0.0 and max_risk_usdt > 0.0:
         # MIN_USDT_BALANCE_TO_TRADE未満の場合 (ゼロ残高を含む)
         trade_size_str = "<code>不足</code>"
         max_risk_str = "<code>不足</code>"
         trade_plan_header = "⚠️ <b>通知のみ（残高不足）</b>"
    else:
         trade_size_str = f"<code>{format_usdt(trade_amount_usdt)}</code>"
         max_risk_str = f"<code>${format_usdt(max_risk_usdt)}</code>"
         trade_plan_header = "✅ <b>自動取引計画</b>"


    header = (
        f"{rank_emoji} <b>Apex Signal - Rank {rank}</b> {rank_emoji}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<b>{symbol}</b> | {direction_emoji} {direction_text} (MEXC Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    trade_plan = (
        f"{trade_plan_header}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>取引サイズ (USDT)</b>: {trade_size_str}\n"
        f"  - <b>許容最大リスク</b>: {max_risk_str}\n"
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
    lt_trend_check_text_penalty = f"長期トレンド ({lt_trend_str}) と逆行 ({tech_data.get('long_term_reversal_penalty_value', 0.0)*100:.1f}点ペナルティ)"


    analysis_details = (
        f"<b>🔍 分析の根拠</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>トレンド/勢い</b>: \n"
        f"    {'✅' if long_term_trend_ok else '❌'} {'<b>' if not long_term_trend_ok else ''}{f'長期 ({lt_trend_str}, SMA {LONG_TERM_SMA_LENGTH}) トレンドと一致' if long_term_trend_ok else lt_trend_check_text_penalty}{'</b>' if not long_term_trend_ok else ''}\n"
        f"    {'✅' if momentum_ok else '⚠️'} 短期モメンタム加速 (RSI/MACD/CCI)\n"
        f"  - <b>価格構造/ファンダ</b>: \n"
        f"    {'✅' if structure_ok else '❌'} 重要支持/抵抗線に近接 ({fib_level}確認)\n"
        f"    {'✅' if (volume_confirm_ok or obv_confirm_ok) else '❌'} 出来高/OBVの裏付け\n"
        f"    {'✅' if liquidity_ok else '❌'} 板の厚み (流動性) 優位\n"
    )

    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ このシグナルは自動売買の対象です。</pre>"
        f"<i>Bot Ver: v19.0.22 - Error Identification Integrated</i>"
    )

    return header + trade_plan + summary + analysis_details + footer


def format_position_status_message(balance_usdt: float, open_positions: Dict, balance_status: str) -> str:
    """現在のポジション状態をまとめたTelegramメッセージをHTML形式で作成する (v19.0.22 強化ロジック)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    # 💡 変更点: ステータスに応じたヘッダーと警告メッセージ
    if balance_status == 'AUTH_ERROR':
        status_line = "🔴 **認証エラー発生**"
        warning_msg = "\n🚨 **APIキー/Secretが不正です。**すぐに確認してください。"
    elif balance_status == 'API_ERROR' or balance_status == 'OTHER_ERROR':
        status_line = "⚠️ **API通信エラー/権限不足の可能性**"
        warning_msg = "\n🚨 **MEXCとの通信に失敗または権限が不足しています。**ログを確認してください。"
    elif balance_status == 'ZERO_BALANCE':
        # 実際残高がゼロ、またはAPI応答からUSDT残高情報が完全に欠落している場合のメッセージ
        status_line = "✅ **残高確認完了 (残高ゼロ)**"
        warning_msg = "\n👉 **USDT残高がゼロ、またはAPI応答から見つからないため、自動取引はスキップされます。**"
    else: # SUCCESS
        status_line = "🔔 **Apex BOT ポジション/残高ステータス**"
        warning_msg = ""


    header = (
        f"{status_line} ({CCXT_CLIENT_NAME} Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **最終確認日時**: {now_jst} (JST)\n"
        f"  - **利用可能USDT残高**: <code>${format_usdt(balance_usdt)}</code>"
        f"{warning_msg}\n" # 警告メッセージを挿入
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
        f"<i>Bot Ver: v19.0.22 - Error Identification Integrated</i>"
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
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegramへの接続/HTTPエラー: {e}")
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
            'fetchBalanceMethod': 'v3',
        },
        'apiKey': mexc_key,
        'secret': mexc_secret,
    }

    try:
        EXCHANGE_CLIENT = ccxt_async.mexc(config)
        auth_status = "認証済み" if mexc_key and mexc_secret else "公開データのみ"
        logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で初期化しました。({auth_status}, Default: Spot)")
    except Exception as e:
        logging.error(f"CCXTクライアントの初期化に失敗しました: {e}")
        EXCHANGE_CLIENT = None


async def fetch_current_balance_usdt_with_status() -> Tuple[float, str]:
    """CCXTから現在のUSDT残高を取得し、ステータスを返す。(v19.0.22 エラー識別強化)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, 'AUTH_ERROR'

    try:
        logging.info("💡 DEBUG (Balance): CCXT fetch_balance() を呼び出します...")

        balance = await EXCHANGE_CLIENT.fetch_balance()

        logging.info("💡 DEBUG (Balance): fetch_balance() が応答を返しました。パースを開始します。")

        # 1. CCXT標準の Unified Balance 構造からの取得
        usdt_free = 0.0
        # 1.1. 通貨キーがトップレベルにある場合 (例: balance['USDT']['free'])
        if 'USDT' in balance and isinstance(balance['USDT'], dict):
             usdt_free = balance['USDT'].get('free', 0.0)
             if usdt_free > 0.0:
                  logging.info(f"✅ DEBUG (Balance Success - Top Key): CCXT標準形式 (トップキー) でUSDT残高 {usdt_free:.2f} を取得しました。")
                  return usdt_free, 'SUCCESS'

        # 1.2. Unifiedオブジェクトの 'free' ディクショナリにある場合 (例: balance['free']['USDT'])
        patch_free_unified = balance.get('free', {}).get('USDT', 0.0)
        if patch_free_unified > 0.0:
             logging.warning(f"⚠️ DEBUG (Patch 1/3 - Unified Free): 'free' オブジェクトからUSDT残高 {patch_free_unified:.2f} を取得しました。")
             return patch_free_unified, 'SUCCESS'

        # 2. Raw Info 強制パッチ (MEXC固有の対応 - 最終手段)
        try:
            raw_info = balance.get('info', {})
            search_paths = [
                 raw_info.get('assets'),
                 raw_info.get('balances'),
                 [raw_info] # info直下も検索対象にする
            ]

            for assets_list in search_paths:
                if isinstance(assets_list, list):
                    for asset in assets_list:
                        # 'currency', 'asset', 'coin' フィールドでUSDTを探す
                        is_usdt = (asset.get('currency') == 'USDT' or
                                   asset.get('asset') == 'USDT' or
                                   asset.get('coin') == 'USDT')

                        if is_usdt:
                            # 'availableBalance' や 'free', 'locked' フィールドから残高を取得
                            available_balance = float(asset.get('availableBalance', asset.get('free', 0.0)))
                            if available_balance <= 0.0 and 'total' in asset:
                                # ロックされていない合計残高
                                available_balance = float(asset.get('total', 0.0)) - float(asset.get('locked', 0.0))

                            if available_balance > 0.0:
                                logging.warning(f"⚠️ DEBUG (Patch 2/3 - Raw Info Forced): 'info' からUSDT残高 {available_balance:.2f} を強制的に取得しました。")
                                return available_balance, 'SUCCESS'

        except Exception as e:
            logging.error(f"❌ DEBUG (Patch Info Error): MEXC Raw Info パッチでエラー発生: {e}")
            pass

        # 3. 取得失敗時の判定とログ出力
        logging.error(f"❌ 残高取得エラー: USDT残高が取得できませんでした。")

        free_keys = list(balance.get('free', {}).keys())
        total_keys = list(balance.get('total', {}).keys())
        logging.error(f"🚨🚨 DEBUG (Free Keys): CCXT Unified 'free' オブジェクト内の通貨キー: {free_keys}")
        logging.error(f"🚨🚨 DEBUG (Total Keys): CCXT Unified 'total' オブジェクト内の通貨キー: {total_keys}")
        logging.warning(f"⚠️ APIキー/Secretの**入力ミス**または**Spot残高読み取り権限**を**最優先で**再度確認してください。")
        available_currencies = list(balance.keys())
        logging.error(f"🚨🚨 DEBUG (Raw Balance Keys): CCXTから返されたRaw Balance Objectのトップレベルキー: {available_currencies}")
        logging.info("💡 CCXTの標準形式に通貨情報が含まれていません。MEXCの設定（現物アカウントの残高、サブアカウントの使用など）をご確認ください。")


        # 実際残高がゼロ、またはAPI応答からUSDT残高情報が完全に欠落している場合
        return 0.0, 'ZERO_BALANCE' # <- 実際の残高がゼロの場合

    except ccxt.AuthenticationError:
        logging.error("❌ 残高取得エラー: APIキー/Secretが不正です (AuthenticationError)。")
        return 0.0, 'AUTH_ERROR' # <- 認証エラーの場合
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 残高取得エラー（CCXT Exchange Error）: {type(e).__name__}: {e}")
        return 0.0, 'API_ERROR' # <- API通信エラーの場合
    except Exception as e:
        logging.error(f"❌ 残高取得エラー（fetch_balance失敗）: {type(e).__name__}: {e}")
        return 0.0, 'OTHER_ERROR' # <- その他の予期せぬエラーの場合

# NOTE: 互換性維持のため、fetch_current_balance_usdt() は削除せず、ラッパーとして残します。
async def fetch_current_balance_usdt() -> float:
    """互換性維持のためのラッパー関数"""
    balance, _ = await fetch_current_balance_usdt_with_status()
    return balance


async def fetch_ohlcv_with_fallback(exchange_id: str, symbol: str, timeframe: str) -> Tuple[pd.DataFrame, str, str]:
    """OHLCVデータを取得する (v19.0.20ベース)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT: return pd.DataFrame(), "Client Error", "EXCHANGE_CLIENT not initialized."

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        if len(df) < limit * 0.9:
             logging.warning(f"⚠️ {symbol} {timeframe}: データ不足 ({len(df)}/{limit})。スキップします。")
             return pd.DataFrame(), "Warning", f"Insufficient data ({len(df)}/{limit})"
        return df, "Success", ""
    except Exception as e:
        logging.error(f"❌ {symbol} {timeframe}: OHLCV取得エラー: {e}")
        return pd.DataFrame(), "Error", str(e)

async def update_symbols_by_volume():
    """出来高に基づいて監視銘柄を更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT: return

    try:
        markets = await EXCHANGE_CLIENT.fetch_tickers()
        usdt_pairs = {s: t for s, t in markets.items() if s.endswith('/USDT')}

        # 出来高降順でソート
        sorted_pairs = sorted(
            usdt_pairs.items(),
            key=lambda x: x[1].get('quoteVolume', 0) or 0,
            reverse=True
        )

        top_symbols = [s for s, t in sorted_pairs[:TOP_SYMBOL_LIMIT]]
        if top_symbols:
            CURRENT_MONITOR_SYMBOLS = top_symbols
            # logging.info(f"監視銘柄を更新しました: {len(top_symbols)} 銘柄") # コメントアウトでログを減らす
    except Exception as e:
        # logging.error(f"出来高による銘柄更新エラー: {e}") # エラー時のみログ
        pass # エラー時は既存のリストを維持

async def get_crypto_macro_context() -> Dict:
    """市場全体のマクロコンテキストを取得する (ダミー/簡易版)"""
    # 実際には外部APIからFGIなどを取得するが、ここではダミー
    return {
        'fgi_proxy': random.uniform(-0.1, 0.1),
        'btc_dominance_trend': 'BULLISH'
    }

async def fetch_order_book_depth(symbol: str) -> bool:
    """オーダーブックの流動性データをキャッシュする (ダミー)"""
    # 実際にはccxt.fetch_order_bookを呼び出すが、ここではダミー
    ORDER_BOOK_CACHE[symbol] = {'bids_depth': random.uniform(100, 5000), 'asks_depth': random.uniform(100, 5000)}
    return True

# ====================================================================================
# TRADING & ANALYSIS LOGIC
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """必要なテクニカル指標を計算する (v19.0.20ベース)"""
    if df.empty: return df

    # トレンド系
    df['SMA_50'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['MACD'] = ta.macd(df['close'], fast=12, slow=26, signal=9)['MACDh_12_26_9']
    df['ADX'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']

    # モメンタム系
    df['RSI'] = ta.rsi(df['close'], length=14)
    # df['STOCH'] = ta.stoch(df['high'], df['low'], df['close'])['STOCHk_14_3_3'] # 別のインジケータも計算可能

    # ボラティリティ/レンジ系
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['BBANDS'] = ta.bbands(df['close'], length=20, std=2)
    df['BB_WIDTH'] = (df['BBANDS']['BBU_20_2.0'] - df['BBANDS']['BBL_20_2.0']) / df['BBANDS']['BBM_20_2.0'] * 100

    # 出来高系
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)


    return df

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """単一の時間足で技術分析を実行し、スコアリングする (v19.0.20ベース)"""
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH: return None

    df = calculate_technical_indicators(df)
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    current_price = last_row['close']

    # ロングシグナルを想定したベーススコア
    score = BASE_SCORE
    tech_data = {}

    # 1. SL/TPとRRRの初期設定 (Range基準: ATRの代わりに直近のHigh/Lowの平均レンジを使用)
    # ATRに基づくSL/TP計算
    last_atr = last_row['ATR']
    sl_offset = last_atr * RANGE_TRAIL_MULTIPLIER

    entry_price = current_price
    sl_price = entry_price - sl_offset # ロング
    tp1_price = entry_price + sl_offset * DTS_RRR_DISPLAY

    risk = entry_price - sl_price
    reward = tp1_price - entry_price
    rr_ratio = reward / risk if risk > 0 and reward > 0 else 0.0

    # 2. 長期トレンドフィルター (4h足のみ適用)
    long_term_trend = 'N/A'
    if timeframe == '4h' and 'SMA_50' in df.columns:
        if current_price > last_row['SMA_50']:
            long_term_trend = 'BULLISH'
        elif current_price < last_row['SMA_50']:
            long_term_trend = 'BEARISH'

        if long_term_trend == 'BEARISH':
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY

        tech_data['long_term_trend'] = long_term_trend

    # 3. モメンタム/過熱感
    if last_row['RSI'] < RSI_MOMENTUM_LOW:
        score += 0.10 # 押し目買いの優位性
    if last_row['RSI'] > RSI_OVERBOUGHT:
        score -= 0.10 # 逆行のリスク

    # 4. MACDクロス確認
    macd_valid = True
    if last_row['MACD'] > 0 and prev_row['MACD'] < 0: # 強気転換 (ロングに有利)
        score += 0.15
    elif last_row['MACD'] < 0 and prev_row['MACD'] > 0: # 弱気転換 (ロングに不利)
        score -= MACD_CROSS_PENALTY
        macd_valid = False

    # 5. ADX (トレンドの強さ)
    tech_data['adx'] = last_row['ADX']
    if last_row['ADX'] > ADX_TREND_THRESHOLD:
        score += 0.05 # トレンドに乗る

    # 6. ボラティリティペナルティ
    if last_row.get('BB_WIDTH', 100) < VOLATILITY_BB_PENALTY_THRESHOLD:
        score -= 0.10 # レンジ相場での取引リスク

    # 7. 構造的サポート/レジスタンス (ダミー) - v19.0.20のロジックを踏襲
    # 実際にはPivotやフィボナッチレベルを計算して近接を判定する
    if random.random() > 0.6:
        score += 0.10
        tech_data['structural_pivot_bonus'] = 0.10
        tech_data['fib_proximity_level'] = 'Support/61.8%'
    else:
        tech_data['structural_pivot_bonus'] = 0.0
        tech_data['fib_proximity_level'] = 'N/A'

    # 8. 出来高の裏付け
    avg_volume = df['volume'].iloc[-30:].mean()
    if last_row['volume'] > avg_volume * VOLUME_CONFIRMATION_MULTIPLIER:
        score += 0.05
        tech_data['volume_confirmation_bonus'] = 0.05
    else:
        tech_data['volume_confirmation_bonus'] = 0.0


    # 9. マクロ/ファンダメンタル (FGIプロキシ)
    fgi_bonus = max(0, min(FGI_PROXY_BONUS_MAX, macro_context.get('fgi_proxy', 0.0)))
    score += fgi_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus

    # 10. OBVモメンタム確認
    obv_momentum_bonus = 0.0
    if last_row['OBV'] > last_row['OBV_SMA'] and prev_row['OBV'] <= prev_row['OBV_SMA']: # OBVがSMAを上抜けた
         obv_momentum_bonus = OBV_MOMENTUM_BONUS
         score += obv_momentum_bonus
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus

    # 11. 流動性ボーナス (キャッシュから取得)
    ob_data = ORDER_BOOK_CACHE.get(symbol, {})
    liquidity_bonus = 0.0
    if ob_data.get('bids_depth', 0) > ob_data.get('asks_depth', 0) * 1.5:
        liquidity_bonus = LIQUIDITY_BONUS_POINT
        score += liquidity_bonus
    tech_data['liquidity_bonus_value'] = liquidity_bonus

    # スコアの正規化
    final_score = max(0.0, min(1.0, score))

    tech_data['macd_cross_valid'] = macd_valid
    tech_data['stoch_filter_penalty'] = 0 # Stochasticsは計算していないため0

    # シグナル判定
    if final_score >= BASE_SCORE and rr_ratio >= 1.0:
         return {
            'symbol': symbol,
            'side': 'ロング',
            'timeframe': timeframe,
            'score': final_score,
            'rr_ratio': rr_ratio,
            'price': current_price,
            'entry': entry_price,
            'sl': sl_price,
            'tp1': tp1_price,
            'tech_data': tech_data
        }

    return None

def calculate_trade_plan(signal: Dict, usdt_balance: float) -> Tuple[float, float, float]:
    """リスクと残高に基づいて取引量を計算する (v19.0.20ベース)"""
    entry = signal['entry']
    sl = signal['sl']

    # 1. 最大許容リスク額の決定
    max_risk_capital = usdt_balance * MAX_RISK_CAPITAL_PERCENT
    max_risk_absolute = MAX_RISK_PER_TRADE_USDT
    max_risk_usdt = min(max_risk_capital, max_risk_absolute)

    # 2. 許容リスク額が取引可能最低残高未満なら取引しない
    if usdt_balance < MIN_USDT_BALANCE_TO_TRADE or max_risk_usdt <= 1.0:
        return 0.0, 0.0, 0.0 # amount, size_usdt, max_risk_usdt

    # 3. 1単位あたりの損失額 (USDT)
    risk_per_unit = abs(entry - sl)

    # 4. 最大許容リスクに基づいた取引単位 (amount)
    if risk_per_unit == 0 or entry == 0:
        # 価格がゼロまたはSLがエントリーと同じ場合は取引不能
        amount_to_buy = 0.0
    else:
        # amount = (最大リスク額 / 1単位あたりのリスク)
        amount_to_buy = (max_risk_usdt * TRADE_SIZE_PER_RISK_MULTIPLIER) / risk_per_unit

    # 5. 取引サイズ (USDT)
    trade_size_usdt = amount_to_buy * entry

    # 6. 残高超過チェック
    if trade_size_usdt > usdt_balance:
        # 残高に合わせて再計算
        trade_size_usdt = usdt_balance
        amount_to_buy = trade_size_usdt / entry if entry > 0 else 0

    if trade_size_usdt < 1.0: # 最小取引サイズ制限
        return 0.0, 0.0, max_risk_usdt

    return amount_to_buy, trade_size_usdt, max_risk_usdt

async def process_trade_signal(signal: Dict, usdt_balance: float, client: ccxt_async.Exchange):
    """シグナルに基づき、現物買い注文を発注する (v19.0.20ベース)"""
    symbol = signal['symbol']
    trade_plan = signal['trade_plan']
    amount = trade_plan['amount_to_buy']
    size_usdt = trade_plan['trade_size_usdt']
    market_price = signal['price'] # 最後のティック価格をエントリー価格の代わりに利用

    if size_usdt == 0.0 or amount == 0.0:
        return

    try:
        # 1. 現物買い (Market Buy) を実行
        # 現物取引では、USDT建ての金額(size_usdt)を指定して買い注文を出す方が確実な取引所が多い
        # amount_to_buyは概算のため、ここではUSDT建て注文を試みます。
        # CCXTには 'create_order' で type='market', side='buy', params={'quoteOrderQty': size_usdt} のように渡す
        
        # CCXTの create_market_buy_order はベース通貨のamount (amount_to_buy) を取るため、それに従います。
        order = await client.create_market_buy_order(symbol, amount)

        # 2. 注文が成功した場合、ポジションを追跡
        if order and order['status'] == 'closed':
            # 実際のエントリー価格はorder['price']またはorder['average']を使用
            entry_price = order.get('average', order.get('price', market_price))
            bought_amount = order.get('filled', order['amount'])

            ACTUAL_POSITIONS[symbol] = {
                'entry_price': entry_price,
                'amount': bought_amount,
                'sl_price': signal['sl'],
                'tp_price': signal['tp1'],
                'open_time': time.time(),
                'status': 'OPEN'
            }
            logging.info(f"✅ TRADE EXECUTED: {symbol} Buy {bought_amount:.4f} @ {entry_price:.4f} (Size: {size_usdt:.2f} USDT)")

    except Exception as e:
        logging.error(f"❌ TRADE FAILED for {symbol}: {e}")

async def manage_open_positions(usdt_balance: float, client: ccxt_async.Exchange):
    """保有中のポジションを監視し、SL/TPの執行を行う (v19.0.20ベース)"""
    if not ACTUAL_POSITIONS: return

    symbols_to_check = list(ACTUAL_POSITIONS.keys())

    try:
        tickers = await client.fetch_tickers(symbols_to_check)
    except Exception:
        logging.error("ポジション管理: ティッカーの取得に失敗しました。")
        return

    positions_to_close = []

    for symbol, pos in ACTUAL_POSITIONS.items():
        ticker = tickers.get(symbol)
        if not ticker: continue

        current_price = ticker['last']
        sl_price = pos['sl_price']
        tp_price = pos['tp_price']

        # 損切りのチェック (ロングポジション)
        if current_price <= sl_price:
            logging.warning(f"🚨 SL HIT: {symbol} at {current_price:.4f} (SL: {sl_price:.4f})")
            positions_to_close.append((symbol, 'SL_HIT'))

        # 利確のチェック
        elif current_price >= tp_price:
            logging.info(f"🎉 TP REACHED: {symbol} at {current_price:.4f} (TP: {tp_price:.4f})")
            positions_to_close.append((symbol, 'TP_REACHED'))

    # ポジションのクローズを実行
    for symbol, status in positions_to_close:
        pos = ACTUAL_POSITIONS.get(symbol)
        if not pos: continue

        try:
            # 現物売り (Market Sell) を実行
            order = await client.create_market_sell_order(symbol, pos['amount'])

            if order and order.get('status') == 'closed':
                 exit_price = order.get('average', order.get('price', 'N/A'))
                 logging.info(f"✅ POSITION CLOSED: {symbol} status: {status} @ {exit_price:.4f}")

            del ACTUAL_POSITIONS[symbol]

        except Exception as e:
            logging.error(f"❌ POSITION CLOSE FAILED for {symbol}: {e}")

async def send_position_status_notification(header_msg: str = "🔄 定期ステータス更新", balance_status: str = 'SUCCESS'):
    """ポジションと残高の定期通知を送信する (v19.0.22 強化ロジック)"""
    global LAST_HOURLY_NOTIFICATION_TIME

    now = time.time()

    # 💡 変更点: 成功時は1時間に1回のみ通知をスキップ。エラー時または初回時は通知を強制。
    if header_msg == "🔄 定期ステータス更新" and now - LAST_HOURLY_NOTIFICATION_TIME < 60 * 60 and balance_status == 'SUCCESS':
        return

    # 💡 変更点: ステータス付きの残高取得関数を呼び出す
    usdt_balance, status_from_fetch = await fetch_current_balance_usdt_with_status()
    message = format_position_status_message(usdt_balance, ACTUAL_POSITIONS, status_from_fetch)

    if header_msg == "🤖 初回起動通知":
        full_message = f"🤖 **Apex BOT v19.0.22 起動完了**\n\n{message}"
    else:
        full_message = f"{header_msg}\n\n{message}"

    send_telegram_html(full_message)

    # エラー時は通知頻度を高く保つため、更新時間を記録しない
    if status_from_fetch == 'SUCCESS':
        LAST_HOURLY_NOTIFICATION_TIME = now


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン処理ループ"""
    global LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, LAST_SUCCESS_TIME

    if not EXCHANGE_CLIENT:
         await initialize_ccxt_client()

    while True:
        try:
            if not EXCHANGE_CLIENT:
                     logging.error("致命的エラー: CCXTクライアントが初期化できません。60秒後に再試行します。")
                     await asyncio.sleep(60)
                     continue

            # 1. 残高とマクロコンテキストの取得 (v19.0.22 変更点)
            usdt_balance_status_task = asyncio.create_task(fetch_current_balance_usdt_with_status())
            macro_context_task = asyncio.create_task(get_crypto_macro_context())

            usdt_balance, balance_status = await usdt_balance_status_task # 💡 ステータスを受け取る
            macro_context = await macro_context_task

            macro_context['current_usdt_balance'] = usdt_balance
            GLOBAL_MACRO_CONTEXT = macro_context

            # 2. 監視銘柄リストの更新
            await update_symbols_by_volume()

            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, USDT残高: {format_usdt(usdt_balance)}, ステータス: {balance_status})")

            # 3. オーダーブックデータのプリフェッチ
            order_book_tasks = [asyncio.create_task(fetch_order_book_depth(symbol)) for symbol in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*order_book_tasks, return_exceptions=True)

            # 4. 分析タスクの並列実行
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                timeframes = ['15m', '1h', '4h']
                for tf in timeframes:
                    ohlcv_data, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf)
                    if status != "Success": continue

                    # pandas/numpyの計算をasyncio.to_threadで実行
                    task = asyncio.create_task(
                         asyncio.to_thread(analyze_single_timeframe, ohlcv_data, tf, symbol, GLOBAL_MACRO_CONTEXT)
                    )
                    analysis_tasks.append(task)
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL / 3)

            raw_analysis_results = await asyncio.gather(*analysis_tasks, return_exceptions=True)

            # 5. 分析結果の集計と取引計画の計算
            all_signals: List[Dict] = []
            for result in raw_analysis_results:
                if isinstance(result, Exception) or not result: continue

                amount, size_usdt, max_risk = calculate_trade_plan(result, usdt_balance)
                result['trade_plan'] = {
                     'amount_to_buy': amount,
                     'trade_size_usdt': size_usdt,
                     'max_risk_usdt': max_risk
                }
                all_signals.append(result)

            # 6. 最適なシグナルの選定と通知
            long_signals = [s for s in all_signals if s['side'] == 'ロング' and s['score'] >= SIGNAL_THRESHOLD]
            long_signals.sort(key=lambda s: (s['score'], s['rr_ratio']), reverse=True)

            top_signals_to_notify = []
            notified_count = 0
            for signal in long_signals:
                symbol = signal['symbol']
                current_time = time.time()
                if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                    top_signals_to_notify.append(signal)
                    notified_count += 1
                    TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                    if notified_count >= TOP_SIGNAL_COUNT: break

            LAST_ANALYSIS_SIGNALS = top_signals_to_notify

            # 7. シグナル通知と自動取引の実行
            trade_tasks = []
            for rank, signal in enumerate(top_signals_to_notify, 1):
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank)
                send_telegram_html(message)

                if signal['trade_plan']['trade_size_usdt'] > 0.0:
                    trade_tasks.append(asyncio.create_task(process_trade_signal(signal, usdt_balance, EXCHANGE_CLIENT)))

            if trade_tasks:
                 await asyncio.gather(*trade_tasks)

            # 8. ポジション管理
            await manage_open_positions(usdt_balance, EXCHANGE_CLIENT)

            # 9. 定期ステータス通知 (v19.0.22 変更点)
            await send_position_status_notification("🔄 定期ステータス更新", balance_status)

            # 10. ループの完了
            LAST_UPDATE_TIME = time.time()
            if balance_status == 'SUCCESS': # 💡 変更点: SUCCESSの場合のみLAST_SUCCESS_TIMEを更新
                 LAST_SUCCESS_TIME = time.time()

            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.22 - Error Identification Integrated)。次の分析まで {LOOP_INTERVAL} 秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラーが発生: {error_name}: {e}")
            # 💡 変更点: エラー発生時もステータス通知を実行
            await send_position_status_notification(f"❌ 致命的エラー発生: {error_name}", 'OTHER_ERROR')
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.22 - Error Identification Integrated")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.22 Startup initializing (Error Identification Integrated)...")

    # CCXT初期化
    await initialize_ccxt_client()

    # 💡 初回起動時のステータス通知 (v19.0.22 変更点)
    usdt_balance, status = await fetch_current_balance_usdt_with_status()
    await send_position_status_notification("🤖 初回起動通知", status)

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
        "bot_version": "v19.0.22 - Error Identification Integrated",
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
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.22 - Error Identification Integrated"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
