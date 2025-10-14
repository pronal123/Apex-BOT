# ====================================================================================
# Apex BOT v19.0.12 - MEXC Balance Logic Fix
# 
# 強化ポイント (v19.0.11からの変更):
# 1. 【MEXC残高強制パッチ修正】`fetch_current_balance_usdt`内の残高取得ロジックを根本的に修正。
#    - `balance['free']['USDT']`がCCXTの標準的なフォールバックパスであり、ログに見られるキー構成(`['info', 'free', 'used', 'total']`)で残高を取得する**最優先のパッチ**となるようにロジックを再構築。
# 2. 【バージョン更新】全てのバージョン情報を v19.0.12 に更新。
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

# 💡 ATR代替として、平均日中変動幅の乗数を使用
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

# ログ設定をDEBUGレベルまで出力するように変更
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
# logging.getLogger().setLevel(logging.DEBUG)
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
    """分析結果を統合したTelegramメッセージをHTML形式で作成する (省略)"""
    # ... (省略: 以前提供したformat_integrated_analysis_message関数全体をここに挿入)
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

    # 💡 v19.0.7 修正：SLソースの表示をATRからRangeに変更
    sl_source_str = "Range基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 Range バッファ"
        
    # 残高不足で取引がスキップされた場合の表示調整
    if trade_amount_usdt == 0.0 and trade_plan_data.get('max_risk_usdt', 0.0) == 0.0:
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
        f"<b>{display_symbol}</b> | {direction_emoji} {direction_text} (MEXC Spot)\n"
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
        f"<i>Bot Ver: v19.0.12 (MEXC Balance Logic Fix)</i>" 
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
        f"<i>Bot Ver: v19.0.12</i>"
    )
    
    return header + details + footer

async def send_position_status_notification(header_msg: str = "🔄 定期ステータス更新"):
    """ポジションと残高の定期通知を送信する"""
    global LAST_HOURLY_NOTIFICATION_TIME
    
    now = time.time()
    if header_msg == "🔄 定期ステータス更新" and now - LAST_HOURLY_NOTIFICATION_TIME < 60 * 60:
        # 1時間に1回のみ定期通知をスキップ
        return

    usdt_balance = await fetch_current_balance_usdt()
    message = format_position_status_message(usdt_balance, ACTUAL_POSITIONS)
    
    if header_msg == "🤖 初回起動通知":
        full_message = f"🤖 **Apex BOT v19.0.12 起動完了**\n\n{message}"
    else:
        full_message = f"{header_msg}\n\n{message}"
        
    send_telegram_html(full_message)
    LAST_HOURLY_NOTIFICATION_TIME = now

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
            # 💡 v19.0.11: MEXCの非標準的な残高取得応答に対応するため、オプションを追加
            'fetchBalanceMethod': 'v3',
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
    """CCXTから現在のUSDT残高を取得する。(v19.0.12 ロジック修正)"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0
        
    try:
        logging.info("💡 DEBUG (Balance): CCXT fetch_balance() を呼び出します...")
        
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        logging.info("💡 DEBUG (Balance): fetch_balance() が応答を返しました。パースを開始します。")
        
        usdt_free = 0.0
        
        # 1. CCXT標準の Unified Balance 構造から残高を取得 (最も信頼性の高い方法)
        
        # 1.1. 通貨キーがトップレベルにある場合 (例: balance['USDT']['free'])
        if 'USDT' in balance and isinstance(balance['USDT'], dict):
             usdt_free = balance['USDT'].get('free', 0.0)
             if usdt_free > 0.0:
                  logging.info(f"✅ DEBUG (Balance Success - Top Key): CCXT標準形式 (トップキー) でUSDT残高 {usdt_free} を取得しました。")
                  return usdt_free
                  
        # 1.2. Unifiedオブジェクトの 'free' ディクショナリにある場合 (例: balance['free']['USDT'])
        # 💡 ログに見られたキー構成 (['info', 'free', 'used', 'total']) の場合の標準的な取得パスです。
        patch_free_unified = balance.get('free', {}).get('USDT', 0.0)
        if patch_free_unified > 0.0:
             logging.warning(f"⚠️ DEBUG (Patch 1/2 - Unified Free): 'free' オブジェクトからUSDT残高 {patch_free_unified} を取得しました。")
             return patch_free_unified
             
        # 2. Raw Info 強制パッチ (MEXC固有の対応 - 最終手段)
        try:
            raw_info = balance.get('info', {})
            # MEXCの現物資産リスト (例: "assets": [{"currency": "USDT", "availableBalance": "123.45", ...}])
            if isinstance(raw_info.get('assets'), list):
                for asset in raw_info['assets']:
                    if asset.get('currency') == 'USDT':
                        available_balance = float(asset.get('availableBalance', 0.0))
                        if available_balance > 0.0:
                            logging.warning(f"⚠️ DEBUG (Patch 2/2 - Raw Info): 'info' -> 'assets' からUSDT残高 {available_balance} を強制的に取得しました。")
                            return available_balance
                            
        except Exception as e:
            logging.warning(f"⚠️ DEBUG (Patch Info Error): MEXC Raw Info パッチでエラー発生: {e}")
            pass

        # 3. 取得失敗時のログ出力と終了
        logging.error(f"❌ 残高取得エラー: USDT残高が取得できませんでした。")
        logging.warning(f"⚠️ APIキー/Secretの**入力ミス**または**Spot残高読み取り権限**、あるいは**MEXCのCCXT形式**を再度確認してください。")
        
        available_currencies = list(balance.keys())
        logging.error(f"🚨🚨 DEBUG (Balance): CCXTから返されたRaw Balance Objectのキー: {available_currencies}")
        
        if available_currencies and len(available_currencies) > 3: 
             other_count = max(0, len(available_currencies) - 5)
             logging.info(f"💡 DEBUG: CCXTから以下の通貨情報が返されました: {available_currencies[:5]}... (他 {other_count} 通貨)")
             logging.info(f"もしUSDTが見当たらない場合、MEXCの**サブアカウント**または**その他のウォレットタイプ**の残高になっている可能性があります。APIキーの設定を確認してください。")
        elif available_currencies:
             logging.info(f"💡 DEBUG: CCXTから以下の通貨情報が返されました: {available_currencies}")
        else:
             logging.info(f"💡 DEBUG: CCXT balance objectが空か、残高情報自体が取得できていません。")

        return 0.0 
        
    except ccxt.AuthenticationError:
        logging.error("❌ 残高取得エラー: APIキー/Secretが不正です (AuthenticationError)。")
        logging.error("🚨🚨 DEBUG (AuthError): 認証エラーが発生しました。APIキー/Secretを確認してください。")
        return 0.0
    except Exception as e:
        # fetch_balance自体が失敗した場合
        logging.error(f"❌ 残高取得エラー（fetch_balance失敗）: {type(e).__name__}: {e}")
        logging.error(f"🚨🚨 DEBUG (OtherError): CCXT呼び出し中に予期せぬエラーが発生しました。詳細: {e}")
        return 0.0

async def update_symbols_by_volume():
    """出来高TOP銘柄を更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT, LAST_SUCCESSFUL_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT:
        return

    try:
        await EXCHANGE_CLIENT.load_markets() 
        
        usdt_tickers = {}
        
        spot_usdt_symbols = [
             symbol for symbol, market in EXCHANGE_CLIENT.markets.items()
             if market['active'] and market['quote'] == 'USDT' and market['spot']
        ]

        symbols_to_check = list(set(DEFAULT_SYMBOLS + spot_usdt_symbols))
        
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

async def get_symbol_info(symbol: str) -> Optional[Dict]:
    """特定のシンボルの詳細情報を取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets:
        return None
    return EXCHANGE_CLIENT.markets.get(symbol)

async def fetch_ohlcv_with_fallback(exchange_name: str, symbol: str, timeframe: str) -> Tuple[Optional[pd.DataFrame], str, Optional[float]]:
    """OHLCVデータを取得し、エラー時にはフォールバックする"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return None, "Error: Client not initialized", None

    # 必須足数の確認
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 300)

    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=required_limit)
        
        if not ohlcv or len(ohlcv) < required_limit:
            return None, f"Error: Data length insufficient ({len(ohlcv)}/{required_limit})", None

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df = df.set_index('timestamp')
        
        current_price = df['close'].iloc[-1]
        
        return df, "Success", current_price

    except Exception as e:
        logging.error(f"OHLCV取得エラー ({symbol} {timeframe}): {e}")
        return None, f"Error: {e}", None

async def fetch_order_book_depth(symbol: str):
    """オーダーブック（板情報）を取得し、キャッシュに保存する"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE
    if not EXCHANGE_CLIENT:
        return
    
    try:
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=ORDER_BOOK_DEPTH_LEVELS)
        ORDER_BOOK_CACHE[symbol] = orderbook
    except Exception as e:
        logging.warning(f"オーダーブック取得エラー ({symbol}): {e}")
        ORDER_BOOK_CACHE[symbol] = None

# ====================================================================================
# ANALYSIS CORE
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算する"""
    df_temp = df.copy()
    
    # 出来高ベースの指標
    df_temp.ta.log_return(cumulative=True, append=True)
    df_temp.ta.obv(append=True)
    df_temp.ta.sma(length=50, append=True)
    
    # モメンタム指標
    df_temp.ta.rsi(append=True)
    df_temp.ta.macd(append=True)
    df_temp.ta.stoch(append=True)
    
    # トレンド/ボラティリティ指標
    df_temp.ta.adx(append=True)
    df_temp.ta.bbands(append=True)
    df_temp.ta.cci(append=True)
    
    # Volaatility Range (ATRの代替として、最近のHigh/Low差の平均を使用)
    df_temp['Range'] = df_temp['high'] - df_temp['low']
    df_temp['AvgRange'] = df_temp['Range'].rolling(window=14).mean()
    
    # Pivot Points (Pivot High/Lowの単純な計算 - 構造的支持/抵抗線)
    df_temp['PivotHigh'] = df_temp['high'].rolling(window=5, center=True).max()
    df_temp['PivotLow'] = df_temp['low'].rolling(window=5, center=True).min()
    
    return df_temp

def calculate_risk_reward_ratio(entry: float, sl: float, tp: float, side: str) -> float:
    """リスクリワード比率 (RRR) を計算する"""
    if side == 'ロング':
        risk = entry - sl
        reward = tp - entry
    elif side == 'ショート': # 現物BOTでは基本的に使用しない
        risk = sl - entry
        reward = entry - tp
    else:
        return 0.0
        
    # リスクが0または負の場合
    if risk <= 0:
        return 0.0
        
    return reward / risk

def get_fibonacci_level(price: float, high: float, low: float) -> Tuple[float, str]:
    """価格がどのフィボナッチレベルに最も近いかを計算する (簡易版)"""
    
    if high <= low: return 0.0, 'N/A'
    
    diff = high - low
    levels = {
        0.0: high, 
        0.236: high - diff * 0.236,
        0.382: high - diff * 0.382,
        0.5: high - diff * 0.5,
        0.618: high - diff * 0.618,
        0.786: high - diff * 0.786,
        1.0: low
    }
    
    # 価格から最も近いフィボナッチレベルを探す
    min_diff = float('inf')
    closest_level = 0.0
    closest_name = 'N/A'
    
    for level_name, level_price in levels.items():
        diff_abs = abs(price - level_price)
        if diff_abs < min_diff:
            min_diff = diff_abs
            closest_level = level_price
            closest_name = f"{level_name*100:.1f}%" if level_name not in [0.0, 1.0] else f"{level_name*100:.0f}%"
            
    # 価格とレベルの差が価格の0.5%以内であれば有効と見なす
    if min_diff / price < 0.005:
        return closest_level, closest_name
    
    return 0.0, 'N/A'


def get_liquidity_bonus(symbol: str, price: float, side: str) -> float:
    """オーダーブックのデータに基づき流動性ボーナスを計算する"""
    orderbook = ORDER_BOOK_CACHE.get(symbol)
    if not orderbook:
        return 0.0
        
    target_book = orderbook['bids'] if side == 'ロング' else orderbook['asks']
    
    if not target_book:
        return 0.0
        
    # 注文価格の5%以内の深さで流動性を評価する（簡易）
    total_depth_usdt = 0.0
    price_tolerance = price * 0.005 # 0.5%以内
    
    for p, amount in target_book:
        if side == 'ロング' and p >= price - price_tolerance and p <= price:
            total_depth_usdt += p * amount
        elif side == 'ショート' and p <= price + price_tolerance and p >= price:
            total_depth_usdt += p * amount
            
    # 流動性が $1,000,000 USDT 以上の場合に最大ボーナスを付与（例）
    if total_depth_usdt > 1_000_000:
        return LIQUIDITY_BONUS_POINT
        
    # それ以外はリニアにボーナスを付与
    return min(LIQUIDITY_BONUS_POINT, (total_depth_usdt / 1_000_000) * LIQUIDITY_BONUS_POINT)


async def get_crypto_macro_context() -> Dict:
    """仮想通貨市場全体のセンチメント（FGIなど）のプロキシをシミュレートする"""
    # 簡易シミュレーション。実際のBOTでは外部APIを使用する。
    try:
        # FGI (Fear & Greed Index) のプロキシとして、BTCの最近の変動幅を評価
        btc_ohlcv, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, "BTC/USDT", "4h")
        if status != "Success":
            return {'sentiment_fgi_proxy_bonus': 0.0}
            
        btc_volatility = btc_ohlcv['close'].iloc[-10:].pct_change().std() * 100
        
        # ボラティリティが高い (0.5%以上) ならリスクオフ (ペナルティ)
        if btc_volatility > 0.5:
            fgi_bonus = -FGI_PROXY_BONUS_MAX # リスクオフペナルティ
        # ボラティリティが低い (0.2%以下) ならリスクオン (ボーナス)
        elif btc_volatility < 0.2:
            fgi_bonus = FGI_PROXY_BONUS_MAX
        else:
            fgi_bonus = 0.0
            
        return {'sentiment_fgi_proxy_bonus': fgi_bonus}
        
    except Exception as e:
        logging.warning(f"マクロコンテキスト取得エラー: {e}")
        return {'sentiment_fgi_proxy_bonus': 0.0}

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """単一の時間足と銘柄に対して分析を実行する"""
    
    if df is None or len(df) < 200:
        return None
        
    df_analyzed = calculate_indicators(df)
    last = df_analyzed.iloc[-1]
    prev = df_analyzed.iloc[-2]
    
    current_price = last['close']
    side = 'ロング' # 現物BOTではロングのみを評価

    score = BASE_SCORE # ベーススコア 0.40
    tech_data = {}
    
    # 1. 損切りの設定 (RangeベースのDTS: Dynamic Trailing Stop)
    avg_range = last['AvgRange']
    range_sl_amount = avg_range * RANGE_TRAIL_MULTIPLIER
    sl_price = current_price - range_sl_amount # ロングの場合
    
    # 2. 構造的SLの検討 (Pivot/Fib)
    structural_sl_used = False
    
    # 0.618/0.786フィボナッチレベルを取得 (ロングエントリーの場合、サポートとして)
    fib_level_price, fib_name = get_fibonacci_level(current_price, df['high'].max(), df['low'].min())
    
    if fib_name != 'N/A' and '61.8%' in fib_name or '78.6%' in fib_name:
         # フィボナッチレベルを構造的なSL候補とする (バッファとして0.5 * Rangeを引く)
         structural_sl_candidate = fib_level_price - avg_range * 0.5
         if structural_sl_candidate < sl_price and structural_sl_candidate > current_price * 0.9: # SLをよりタイトにできる場合
             sl_price = structural_sl_candidate
             structural_sl_used = True
             
    tech_data['structural_sl_used'] = structural_sl_used
    tech_data['fib_proximity_level'] = fib_name
    
    # 3. リスク額の計算
    risk_amount = current_price - sl_price
    if risk_amount <= current_price * 0.005: # リスクが小さすぎる (0.5%未満) 場合は除外
         return None 
         
    # 4. 利確の設定 (RRR = DTS_RRR_DISPLAY を満たすように設定)
    tp1_price = current_price + risk_amount * DTS_RRR_DISPLAY
    rr_ratio = DTS_RRR_DISPLAY
    
    # --- スコアリング ---
    
    # 5. 長期トレンドフィルター (4h足の50SMA)
    if timeframe == '4h':
        sma_50 = last['SMA_50']
        long_term_trend = 'Up' if current_price > sma_50 else ('Down' if current_price < sma_50 else 'Sideways')
        tech_data['long_term_trend'] = long_term_trend
        
        # 長期トレンドと逆行する場合にペナルティ
        long_term_reversal_penalty = False
        if side == 'ロング' and long_term_trend == 'Down':
             score -= LONG_TERM_REVERSAL_PENALTY
             long_term_reversal_penalty = True
             tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
             
        tech_data['long_term_reversal_penalty'] = long_term_reversal_penalty
        
    # 6. モメンタム (RSI, MACD, Stoch)
    
    # RSI (40以下で買いモメンタム候補)
    if last['RSI_14'] < RSI_MOMENTUM_LOW:
         score += 0.08
    elif last['RSI_14'] > RSI_MOMENTUM_HIGH: # 高すぎるとペナルティ (行き過ぎ)
         score -= 0.05
         
    # MACDクロス確認 (MACDラインがシグナルラインを上抜け)
    macd_cross_valid = last['MACDh_12_26_9'] > 0 and prev['MACDh_12_26_9'] < 0
    if macd_cross_valid:
         score += 0.10
    else:
         score -= MACD_CROSS_PENALTY
         
    tech_data['macd_cross_valid'] = macd_cross_valid
    
    # Stochastics (20以下で買いモメンタム候補)
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    stoch_filter_penalty = 0
    if stoch_k < 20 and stoch_d < 20:
         score += 0.05
    elif stoch_k > 80 or stoch_d > 80: # 売られすぎが解消済み/買われすぎ
         score -= 0.10
         stoch_filter_penalty = 1
         
    tech_data['stoch_filter_penalty'] = stoch_filter_penalty
         
    # 7. トレンドの強さ (ADX)
    adx = last['ADX_14']
    tech_data['adx'] = adx
    if adx > ADX_TREND_THRESHOLD:
        score += 0.05
        
    # 8. 出来高の確証 (過去20期間の平均出来高のX倍以上)
    avg_volume = df['volume'].rolling(window=20).mean().iloc[-2]
    volume_confirmation_bonus = 0.0
    if last['volume'] > avg_volume * VOLUME_CONFIRMATION_MULTIPLIER:
         volume_confirmation_bonus = 0.05
         score += volume_confirmation_bonus
         
    tech_data['volume_confirmation_bonus'] = volume_confirmation_bonus
    
    # 9. OBVの確証 (OBVが上昇トレンド)
    obv_momentum_bonus_value = 0.0
    if last['OBV'] > prev['OBV']:
         obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
         score += obv_momentum_bonus_value
         
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # 10. 価格構造 (Pivot/Fibからの反発)
    structural_pivot_bonus = 0.0
    if structural_sl_used:
         structural_pivot_bonus = 0.05
         score += structural_pivot_bonus
         
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
         
    # 11. 流動性ボーナス
    liquidity_bonus_value = get_liquidity_bonus(symbol, current_price, side)
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # 12. マクロセンチメント
    sentiment_fgi_proxy_bonus = macro_context.get('sentiment_fgi_proxy_bonus', 0.0)
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # スコアを正規化 (最大スコアは1.00)
    final_score = min(1.00, score)
    
    # 結果を辞書形式でまとめる
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'price': current_price,
        'entry': current_price,
        'sl': sl_price,
        'tp1': tp1_price,
        'rr_ratio': rr_ratio,
        'tech_data': tech_data,
        'trade_plan': {} # 後で `calculate_trade_plan` が挿入
    }

# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

def calculate_trade_plan(signal: Dict, usdt_balance: float) -> Tuple[float, float, float]:
    """シグナルに基づき、取引量とリスク額を計算する"""
    
    current_price = signal['entry']
    sl_price = signal['sl']
    
    if usdt_balance < MIN_USDT_BALANCE_TO_TRADE:
         # 残高不足の場合、取引サイズは0、リスク額も0
         return 0.0, 0.0, 0.0

    # 1. 1単位あたりのリスク (USDT)
    risk_per_unit = current_price - sl_price
    if risk_per_unit <= 0:
        return 0.0, 0.0, 0.0

    # 2. 許容最大リスク額 (USDT)
    max_risk_usdt_by_capital = usdt_balance * MAX_RISK_CAPITAL_PERCENT
    max_risk_usdt = min(MAX_RISK_PER_TRADE_USDT, max_risk_usdt_by_capital)
    
    # 3. 許容リスク額に基づいた取引単位数 (Amount)
    amount_to_buy = (max_risk_usdt / risk_per_unit) * TRADE_SIZE_PER_RISK_MULTIPLIER
    
    # 4. USDT換算での取引サイズ
    trade_size_usdt = amount_to_buy * current_price
    
    # 5. 利用可能残高を超えないように調整
    if trade_size_usdt > usdt_balance:
        trade_size_usdt = usdt_balance * 0.99 # バッファを設ける
        amount_to_buy = trade_size_usdt / current_price
        max_risk_usdt = amount_to_buy * risk_per_unit # リスクも再計算

    return amount_to_buy, trade_size_usdt, max_risk_usdt


async def process_trade_signal(signal: Dict, usdt_balance: float, client: ccxt_async.Exchange):
    """シグナルを基に実際の取引を実行する"""
    
    global ACTUAL_POSITIONS
    symbol = signal['symbol']
    
    if symbol in ACTUAL_POSITIONS:
        logging.warning(f"⚠️ {symbol} は既にオープンポジションがあるため、取引をスキップします。")
        return
        
    amount_to_buy, trade_size_usdt, max_risk_usdt = calculate_trade_plan(signal, usdt_balance)
    
    if amount_to_buy == 0.0:
        if usdt_balance >= MIN_USDT_BALANCE_TO_TRADE:
             logging.warning(f"⚠️ {symbol} の計算された取引サイズが0または小さすぎるため、取引をスキップします。")
        return

    try:
        # 1. 取引量/価格の丸め込み
        market = await client.load_markets()
        params = market[symbol]['limits']
        
        # 数量を丸める
        amount_to_buy = client.amount_to_precision(symbol, amount_to_buy)
        
        # 2. 買い注文の実行 (Market Buy)
        order = await client.create_market_buy_order(symbol, amount_to_buy)
        
        # 3. 注文情報の確認とポジションの記録
        if order['status'] == 'closed' and order['filled'] > 0:
            filled_amount = order['filled']
            entry_price = order['average']
            
            # ポジションの記録
            ACTUAL_POSITIONS[symbol] = {
                'entry_price': entry_price,
                'amount': filled_amount,
                'sl_price': signal['sl'],
                'tp_price': signal['tp1'],
                'open_time': time.time(),
                'status': 'Open'
            }
            
            logging.info(f"✅ SPOT BUY成功: {symbol} | 数量: {filled_amount:.4f} | 平均価格: {format_price_utility(entry_price, symbol)}")
            send_telegram_html(f"🎉 **BUY実行成功**\n\n🔹 {symbol} 現物買い完了\n  - Buy @ <code>${format_price_utility(entry_price, symbol)}</code>\n  - SL/TPはシステムが管理します。")
            
        else:
            logging.error(f"❌ BUY注文失敗または未約定: {symbol} | 注文ステータス: {order.get('status', 'N/A')}")

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー ({symbol}): {e}")
    except Exception as e:
        logging.error(f"❌ 取引実行中に予期せぬエラーが発生: {e}")

async def close_position(symbol: str, position: Dict, reason: str, client: ccxt_async.Exchange):
    """ポジションを決済する (Market Sell)"""
    global ACTUAL_POSITIONS
    
    try:
        # 1. 売却数量の確認
        amount_to_sell = position['amount']
        
        if amount_to_sell <= 0:
            logging.error(f"❌ {symbol} の決済失敗: 数量が不正です。")
            del ACTUAL_POSITIONS[symbol]
            return

        # 2. 数量の丸め込み
        amount_to_sell = client.amount_to_precision(symbol, amount_to_sell)
        
        # 3. 売り注文の実行 (Market Sell)
        order = await client.create_market_sell_order(symbol, amount_to_sell)
        
        # 4. 決済情報の確認
        if order['status'] == 'closed' and order['filled'] > 0:
            closed_amount = order['filled']
            exit_price = order['average']
            
            # PnL計算 (簡易)
            pnl_usdt = (exit_price - position['entry_price']) * closed_amount
            pnl_percent = (exit_price / position['entry_price'] - 1) * 100
            
            pnl_sign = "🟢 利益確定" if pnl_usdt >= 0 else "🔴 損切り"
            
            logging.info(f"✅ SPOT SELL成功 ({reason}): {symbol} | 決済価格: {format_price_utility(exit_price, symbol)} | PnL: {pnl_usdt:.2f} USDT ({pnl_percent:.2f}%)")
            send_telegram_html(f"🔥 **{pnl_sign}**\n\n🔹 **{symbol}** 現物ポジション決済完了\n  - 理由: {reason}\n  - 決済価格: <code>${format_price_utility(exit_price, symbol)}</code>\n  - PnL (USDT): <code>{pnl_usdt:+.2f}</code> (<code>{pnl_percent:+.2f}%</code>)")
            
            del ACTUAL_POSITIONS[symbol]
            
        else:
            logging.error(f"❌ SELL注文失敗または未約定: {symbol} | 注文ステータス: {order.get('status', 'N/A')}")
            
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 決済時の取引所エラー ({symbol}): {e}")
    except Exception as e:
        logging.error(f"❌ 決済実行中に予期せぬエラーが発生: {e}")
        

async def manage_open_positions(usdt_balance: float, client: ccxt_async.Exchange):
    """オープンポジションを監視し、SL/TPに達したら決済する"""
    
    if not ACTUAL_POSITIONS:
        return
        
    symbols_to_check = list(ACTUAL_POSITIONS.keys())
    
    # 最新の価格を取得 (1h足で十分)
    price_cache: Dict[str, float] = {}
    price_tasks = []
    for symbol in symbols_to_check:
        # OHLCV取得タスクを作成 (価格更新が目的のため、短い期間で十分)
        price_tasks.append(asyncio.create_task(fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '1h')))
        
    price_results = await asyncio.gather(*price_tasks, return_exceptions=True)
    
    # 取得結果から価格をキャッシュに格納 (シンボル名を適切に取得するロジックを簡略化)
    for symbol, result in zip(symbols_to_check, price_results):
        if isinstance(result, Tuple) and result[1] == "Success":
            _, _, price = result
            if price is not None:
                price_cache[symbol] = price
    
    
    # 決済ロジック
    for symbol, pos in list(ACTUAL_POSITIONS.items()):
        
        current_price = price_cache.get(symbol)
        if current_price is None:
            logging.warning(f"⚠️ {symbol} の現在価格を取得できませんでした。監視をスキップします。")
            continue
            
        sl_price = pos['sl_price']
        tp_price = pos['tp_price']
        
        # 損切り判定 (現物ロングの場合: 現在価格 < SL価格)
        if current_price <= sl_price:
            logging.info(f"🚨 SLトリガー: {symbol} ({format_price_utility(current_price, symbol)} <= {format_price_utility(sl_price, symbol)})")
            await close_position(symbol, pos, "損切り (SL到達)", client)
            
        # 利確判定 (現物ロングの場合: 現在価格 >= TP価格)
        elif current_price >= tp_price:
            logging.info(f"💰 TPトリガー: {symbol} ({format_price_utility(current_price, symbol)} >= {format_price_utility(tp_price, symbol)})")
            await close_position(symbol, pos, "利益確定 (TP到達)", client)
            
        else:
            # ポジションを更新
            ACTUAL_POSITIONS[symbol]['status'] = f"Open @ {format_price_utility(current_price, symbol)}"
            
    # 定期ステータス通知
    await send_position_status_notification()

# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン処理ループ"""
    global LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, LAST_SUCCESS_TIME
    
    while True:
        try:
            # 1. CCXTクライアントの準備
            if not EXCHANGE_CLIENT:
                await initialize_ccxt_client()
                if not EXCHANGE_CLIENT:
                     logging.error("致命的エラー: CCXTクライアントが初期化できません。60秒後に再試行します。")
                     await asyncio.sleep(60)
                     continue

            # 2. 残高とマクロコンテキストの取得 (並列実行)
            usdt_balance_task = asyncio.create_task(fetch_current_balance_usdt())
            macro_context_task = asyncio.create_task(get_crypto_macro_context())
            
            usdt_balance = await usdt_balance_task
            macro_context = await macro_context_task
            
            macro_context['current_usdt_balance'] = usdt_balance
            GLOBAL_MACRO_CONTEXT = macro_context
            
            # 3. 監視銘柄リストの更新
            await update_symbols_by_volume()
            
            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, USDT残高: {format_usdt(usdt_balance)})")
            
            # 4. オーダーブックデータのプリフェッチ (流動性ボーナスに使用)
            order_book_tasks = [asyncio.create_task(fetch_order_book_depth(symbol)) for symbol in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*order_book_tasks, return_exceptions=True) 
            
            # 5. 分析タスクの並列実行
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                
                # 15m, 1h, 4h のOHLCV取得と分析を並列で実行
                timeframes = ['15m', '1h', '4h']
                for tf in timeframes:
                    # OHLCV取得
                    ohlcv_data, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf)
                    if status != "Success":
                        continue
                        
                    # 分析タスクの作成
                    task = asyncio.create_task(
                         asyncio.to_thread(analyze_single_timeframe, ohlcv_data, tf, symbol, GLOBAL_MACRO_CONTEXT)
                    )
                    analysis_tasks.append(task)
                    
                    # APIレート制限対策 (非同期で遅延)
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL / 3) 

            # 全ての分析タスクの完了を待機
            raw_analysis_results = await asyncio.gather(*analysis_tasks, return_exceptions=True)
            
            # 6. 分析結果の集計とフィルタリング
            all_signals: List[Dict] = []
            for result in raw_analysis_results:
                if isinstance(result, Exception):
                    error_name = type(result).__name__
                    continue
                if result:
                    # 取引計画の計算と挿入
                    amount, size_usdt, max_risk = calculate_trade_plan(result, usdt_balance)
                    result['trade_plan'] = {
                         'amount_to_buy': amount,
                         'trade_size_usdt': size_usdt,
                         'max_risk_usdt': max_risk
                    }
                    all_signals.append(result)
            
            # 7. 最適なシグナルの選定と通知
            long_signals = [s for s in all_signals if s['side'] == 'ロング' and s['score'] >= SIGNAL_THRESHOLD]
            long_signals.sort(key=lambda s: (s['score'], s['rr_ratio']), reverse=True)
            
            top_signals_to_notify = []
            notified_count = 0
            
            for signal in long_signals:
                symbol = signal['symbol']
                current_time = time.time()
                
                last_notify_time = TRADE_NOTIFIED_SYMBOLS.get(symbol, 0)
                if current_time - last_notify_time > TRADE_SIGNAL_COOLDOWN:
                    top_signals_to_notify.append(signal)
                    notified_count += 1
                    TRADE_NOTIFIED_SYMBOLS[symbol] = current_time 
                    if notified_count >= TOP_SIGNAL_COUNT:
                        break
                        
            LAST_ANALYSIS_SIGNALS = top_signals_to_notify
            
            # 8. シグナル通知と自動取引の実行
            trade_tasks = []
            for rank, signal in enumerate(top_signals_to_notify, 1):
                # Telegram通知
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank)
                send_telegram_html(message)
                
                # 自動取引の実行 (非同期)
                if signal['trade_plan']['trade_size_usdt'] > 0.0:
                    trade_tasks.append(asyncio.create_task(process_trade_signal(signal, usdt_balance, EXCHANGE_CLIENT)))
                    
            if trade_tasks:
                 await asyncio.gather(*trade_tasks)
            
            # 9. ポジション管理
            await manage_open_positions(usdt_balance, EXCHANGE_CLIENT)

            # 10. ループの完了
            LAST_UPDATE_TIME = time.time()
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.12)。次の分析まで {LOOP_INTERVAL} 秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            
            if error_name != 'Exception' or not str(e).startswith("残高取得エラー"):
                 logging.error(f"メインループで致命的なエラー: {error_name}: {e}")
            
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.12 - MEXC Balance Logic Fix")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.12 Startup initializing (MEXC Balance Logic Fix)...") 
    
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
        "bot_version": "v19.0.12 - MEXC Balance Logic Fix",
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
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.12 - MEXC Balance Logic Fix"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=os.environ.get("PORT", 8000))
