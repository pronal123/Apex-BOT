# ====================================================================================
# Apex BOT v19.0.10 - Extreme Balance Debug
# 
# 強化ポイント (v19.0.9からの変更):
# 1. 【残高デバッグログの極限強化】`fetch_current_balance_usdt`関数に、API呼び出し前、呼び出し後のRawキー、認証エラー時、一般エラー時の詳細なデバッグログ（🚨🚨 DEBUG）を追加し、エラー箇所の特定を支援する。
# 2. 【バージョン更新】全てのバージョン情報を v19.0.10 に更新。
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
# デバッグ情報を詳細に見たい場合は、以下を logging.DEBUG に変更
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
        f"<i>Bot Ver: v19.0.10 (Extreme Balance Debug)</i>" 
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
        f"<i>Bot Ver: v19.0.10</i>"
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
        # 💡 DEBUG 1: API呼び出し直前にログを挿入
        logging.info("💡 DEBUG (Balance): CCXT fetch_balance() を呼び出します...")
        
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 💡 DEBUG 2: API呼び出しが成功し、残高オブジェクトを取得したことを確認
        logging.info("💡 DEBUG (Balance): fetch_balance() が応答を返しました。パースを開始します。")
        
        # SpotアカウントのUSDT残高を取得 (freeを使用)
        usdt_free = balance.get('USDT', {}).get('free', 0.0)
        
        # 💡 USDTキーが存在しない場合のチェックとデバッグログを強化
        if 'USDT' not in balance:
            # USDT残高情報が取得できない場合、環境設定の問題の可能性が高い
            logging.error(f"❌ 残高取得エラー: `fetch_balance`の結果に'USDT'キーが見つかりませんでした。")
            logging.warning(f"⚠️ APIキー/Secretの**入力ミス**または**Spot残高読み取り権限**、あるいは**MEXCのCCXT形式**を再度確認してください。")
            
            # デバッグのために balance オブジェクトのキーをログ出力
            available_currencies = list(balance.keys())
            
            # 🚨🚨 DEBUG ログ (最重要): 返されたRaw Balance Objectのトップレベルのキーを出力
            logging.error(f"🚨🚨 DEBUG (Balance): CCXTから返されたRaw Balance Objectのキー: {available_currencies}")
            
            # v19.0.9の既存のログ
            if available_currencies and len(available_currencies) > 3: # 複数の通貨が返されている場合
                 # -1通貨のエラーを回避するため、表示ロジックを修正
                 other_count = max(0, len(available_currencies) - 5)
                 logging.info(f"💡 DEBUG: CCXTから以下の通貨情報が返されました: {available_currencies[:5]}... (他 {other_count} 通貨)")
                 logging.info(f"もしUSDTが見当たらない場合、MEXCの**サブアカウント**または**その他のウォレットタイプ**の残高になっている可能性があります。APIキーの設定を確認してください。")
            elif available_currencies:
                 logging.info(f"💡 DEBUG: CCXTから以下の通貨情報が返されました: {available_currencies}")
            else:
                 logging.info(f"💡 DEBUG: CCXT balance objectが空か、残高情報自体が取得できていません。")

            return 0.0 # 0.0を返してBOTは継続させる。

        # USDTキーは存在するが残高が0の場合
        if usdt_free == 0.0:
            logging.warning(f"⚠️ USDT残高 (free) は0.0です。取引は監視のみとなります。")
        
        return usdt_free
        
    except ccxt.AuthenticationError:
        logging.error("❌ 残高取得エラー: APIキー/Secretが不正です (AuthenticationError)。")
        # 🚨🚨 DEBUG ログ 3: 認証エラー時にログ
        logging.error("🚨🚨 DEBUG (AuthError): 認証エラーが発生しました。APIキー/Secretを確認してください。")
        return 0.0
    except Exception as e:
        # fetch_balance自体が失敗した場合
        logging.error(f"❌ 残高取得エラー（fetch_balance失敗）: {type(e).__name__}: {e}")
        # 🚨🚨 DEBUG ログ 4: その他のエラー時にログ
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
        # v19.0.7: 強制数値変換 (ATR問題回避のため)
        df_btc['close'] = pd.to_numeric(df_btc['close'], errors='coerce').astype('float64')
        if not df_btc['close'].isna().all():
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
        # v19.0.7: 強制数値変換 (ATR問題回避のため)
        df_eth['close'] = pd.to_numeric(df_eth['close'], errors='coerce').astype('float64')
        if not df_eth['close'].isna().all():
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
                # CCXTのオーダーブックデータは既に数値型であるはずだが、念のため型チェック
                try:
                    total_usdt += float(price) * float(amount)
                except ValueError:
                    logging.warning(f"⚠️ {symbol} オーダーブックデータが不正です。")
                    return 0.0
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
# CORE ANALYSIS & TRADE EXECUTION LOGIC (v19.0.10 - No ATR Logic)
# ====================================================================================

# 💡 ATRを使用しない代替関数
def analyze_structural_proximity_no_atr(df: pd.DataFrame, price: float, side: str, avg_range: float) -> Tuple[float, float, bool, str]:
    """構造的な支持/抵抗線の近接度を分析し、SL価格とボーナスを計算する (ATR代替)"""
    
    # avg_rangeが0の場合のフォールバック (致命的エラーを防ぐ)
    if avg_range <= 0.0:
        avg_range = price * 0.001 # 0.1%を最低ボラティリティとして設定
        
    # 20期間の最安値（ロングの構造的SL候補）
    pivot_long = df['low'].rolling(window=20).min().iloc[-1]
    
    structural_sl_used = False
    
    if side == 'ロング':
        structural_sl_candidate = pivot_long
        
        if structural_sl_candidate > 0:
             # 構造的サポート（ピボット）をSLの基準とし、0.5 * avg_range のバッファを持たせる
             sl_price = structural_sl_candidate - (0.5 * avg_range) 
             structural_sl_used = True
        else:
             # 構造的候補がない場合はavg_range基準
             sl_price = price - (RANGE_TRAIL_MULTIPLIER * avg_range)
             
    else: 
        # ショートシグナルの場合は、いったんRange基準SLを返す
        return 0.0, price + (RANGE_TRAIL_MULTIPLIER * avg_range), False, 'N/A' # ショートの場合はSLは上

    bonus = 0.0
    fib_level = 'N/A'
    
    if side == 'ロング':
        # 現在価格が構造的サポート（pivot_long）に近接しているかチェック
        distance = price - pivot_long
        
        if 0 < distance <= 2.5 * avg_range:
            # 2.5 * avg_range以内に重要なサポートがある場合ボーナス
            bonus += 0.08 
            fib_level = "Support Zone"
            
        # 4h足SMA50 (SMA) に価格が近接しているかチェック (トレンドと一致する押し目買いの確認)
        sma_long = df['sma'].iloc[-1] if 'sma' in df.columns and not df['sma'].isna().iloc[-1] else None
        if sma_long and price >= sma_long and price - sma_long < 3 * avg_range:
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
    
    # 🌟 v19.0.6 修正ポイント: 全てのOHLCVカラムを強制的にfloatに変換し、データ品質チェックを行う 🌟
    # errors='coerce'で数値に変換できない値をNaNにする
    for col in ['close', 'high', 'low', 'open', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce').astype('float64') 

    # 必須のOHLCVデータが全てNaNになっていないかチェック
    if df['close'].isna().all() or df['volume'].isna().all():
        logging.error(f"❌ {symbol} {timeframe}: OHLCVの数値変換後、終値または出来高が全てNaNになりました。APIからのデータが不正です。分析をスキップ。")
        return None
    # ---------------------------------------------------------------------------------
    
    # 💡 v19.0.7 修正ポイント: ATRの代わりにボラティリティの近似を計算
    # 過去20期間のTrue Rangeの近似 (High - Low) の平均を使用
    df['Range'] = df['high'] - df['low']
    avg_range = df['Range'].rolling(window=20).mean().iloc[-1] if len(df) >= 20 else df['Range'].iloc[-1]
    
    # テクニカル指標の計算
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.adx(append=True)
    df.ta.stoch(append=True)
    df.ta.cci(append=True)
    df.ta.bbands(append=True) 
    # df.ta.atr(length=14, append=True) # <- ATRの計算を削除 (v19.0.7)
    df.ta.obv(append=True) 
    df['sma'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 
    
    df.dropna(inplace=True)
    
    # 🌟 v19.0.5 修正ポイント: dropna後の行数チェックを強化 🌟
    REQUIRED_ROWS_AFTER_NAN = 30 
    if len(df) < REQUIRED_ROWS_AFTER_NAN:
        logging.warning(f"⚠️ {symbol} {timeframe}: dropna後にデータが{len(df)}行しか残りませんでした。分析をスキップします。")
        return None
        
    # avg_range の再チェック (計算後にNaNになる可能性は低いが、安全策として)
    if avg_range <= 0.0 or np.isnan(avg_range):
        logging.warning(f"⚠️ {symbol} {timeframe}: ボラティリティの近似計算に失敗しました。この時間足の分析をスキップします。")
        return None
    
    latest = df.iloc[-1]
    price = latest['close']
    
    rsi = latest['RSI_14']
    macd_hist = latest['MACDh_12_26_9']
    adx = latest['ADX_14']
    stoch_k = latest['STOCHk_14_3_3']
    stoch_d = latest['STOCHd_14_3_3']
    cci = latest['CCI_14_0.015']
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

        # SL/TPの計算と構造的サポートボーナス (ATR代替を使用)
        struct_bonus, sl_price, structural_sl_used, fib_level = analyze_structural_proximity_no_atr(df, price, side, avg_range)
        score += struct_bonus
        tech_data['structural_pivot_bonus'] = struct_bonus
        tech_data['structural_sl_used'] = structural_sl_used
        tech_data['fib_proximity_level'] = fib_level
        
        risk_dist = price - sl_price
        if risk_dist <= 0: 
            return None 
        
        # TP価格をリスクリワード比率に基づいて決定
        tp1_price = price + (risk_dist * DTS_RRR_DISPLAY)

        # 💡 流動性ボーナスの適用
        liquidity_data = ORDER_BOOK_CACHE.get(symbol)
        liquidity_bonus = 0.0
        if liquidity_data:
            # 買い注文の深度（bids）が売り注文の深度（asks）より厚い場合、ロングのボーナス
            if liquidity_data['bids_usdt'] > liquidity_data['asks_usdt'] * 1.2: # 20%以上厚い
                liquidity_bonus = LIQUIDITY_BONUS_POINT
            score += liquidity_bonus
            tech_data['liquidity_bonus_value'] = liquidity_bonus
            
        # 💡 FGIプロキシボーナスの適用 (Longシグナルに対してのみ)
        fgi_proxy_score = macro_context.get('sentiment_fgi_proxy', 0.0)
        if fgi_proxy_score > 0.0:
            score += fgi_proxy_score
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy_score

        # 最終的なスコアをクリップ
        final_score = min(1.0, max(BASE_SCORE, score))
        tech_data['adx'] = adx 

        # 💡 取引プラン計算
        current_usdt_balance = macro_context.get('current_usdt_balance', 0.0)
        max_risk_usdt = min(MAX_RISK_PER_TRADE_USDT, current_usdt_balance * MAX_RISK_CAPITAL_PERCENT)

        # USDT残高が不足している場合、リスクと取引サイズは0
        if current_usdt_balance < MIN_USDT_BALANCE_TO_TRADE:
            max_risk_usdt = 0.0
            trade_amount = 0.0
            trade_size_usdt = 0.0
            # 残高が閾値以下の場合は警告を出力
            # v19.0.8: 残高取得に失敗している場合（current_usdt_balance=0.0）は、fetch_balance側で警告済みのため、ここでは重複を避ける
            if current_usdt_balance < MIN_USDT_BALANCE_TO_TRADE - 0.01 and current_usdt_balance > 0.0:
                logging.warning(f"⚠️ {symbol} {timeframe}: USDT残高が不足しています ({format_usdt(current_usdt_balance)} < {format_usdt(MIN_USDT_BALANCE_TO_TRADE)})。取引をスキップし、監視のみ実行します。")
        else:
            # 許容リスク額から購入単位を計算: (リスク額 / (現在価格 - SL価格)) * リスク乗数
            if risk_dist > 0 and price > 0:
                trade_amount = (max_risk_usdt / risk_dist) * TRADE_SIZE_PER_RISK_MULTIPLIER
                trade_size_usdt = trade_amount * price
            else:
                trade_amount = 0.0
                trade_size_usdt = 0.0
                
        trade_plan = {
            'max_risk_usdt': max_risk_usdt,
            'trade_amount_units': trade_amount,
            'trade_size_usdt': trade_size_usdt
        }
        
        return {
            'symbol': symbol,
            'side': side,
            'timeframe': timeframe,
            'price': price,
            'score': final_score,
            'entry': price, # 成行注文を想定
            'sl': sl_price,
            'tp1': tp1_price,
            'risk_dist': risk_dist,
            'tp1_dist': tp1_price - price,
            'rr_ratio': (tp1_price - price) / risk_dist,
            'tech_data': tech_data,
            'trade_plan': trade_plan
        }
        
    return None

async def process_trade_signal(signal: Dict, balance_usdt: float, client: ccxt_async.Exchange):
    """シグナルに基づいた自動取引を実行する"""
    
    # 💡 現物ポジション管理ロジックをここに実装
    
    # ログ出力のみ
    symbol = signal['symbol']
    score = signal['score']
    trade_plan = signal['trade_plan']
    trade_size_usdt = trade_plan['trade_size_usdt']
    
    if trade_size_usdt > 0.0:
        logging.info(f"💰 {symbol} ({signal['timeframe']}): Score {score:.2f}。取引を実行します: ${format_usdt(trade_size_usdt)} (リスク: ${format_usdt(trade_plan['max_risk_usdt'])})")
        # 実際の発注ロジック (ccxt.create_order) はこの後ろに実装
        
        # 💡 ポジション管理のダミー更新
        global ACTUAL_POSITIONS
        ACTUAL_POSITIONS[symbol] = {
            'entry_price': signal['entry'],
            'amount': trade_plan['trade_amount_units'],
            'sl_price': signal['sl'],
            'tp_price': signal['tp1'],
            'open_time': time.time(),
            'status': 'OPEN'
        }

    
async def manage_open_positions(balance_usdt: float, client: ccxt_async.Exchange):
    """保有中のポジションを管理し、決済条件をチェックする"""
    global ACTUAL_POSITIONS
    
    # 💡 リアルタイム価格の取得
    if not ACTUAL_POSITIONS:
        return
        
    symbols_to_fetch = list(ACTUAL_POSITIONS.keys())
    
    try:
        tickers = await client.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.error(f"ポジション管理中の価格取得エラー: {e}")
        return

    # 決済ロジック
    closed_positions = {}
    for symbol, pos in ACTUAL_POSITIONS.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
            
        current_price = ticker['last']
            
        # SL/TPチェック
        sl_price = pos['sl_price']
        tp_price = pos['tp_price']
        
        # 損切り (SL)
        if current_price <= sl_price:
            closed_positions[symbol] = {'status': 'SL_HIT', 'price': current_price}
        
        # 利確 (TP)
        elif current_price >= tp_price:
            closed_positions[symbol] = {'status': 'TP_HIT', 'price': current_price}

    # 決済の実行 (ここではログ出力のみ)
    for symbol, data in closed_positions.items():
        del ACTUAL_POSITIONS[symbol]
        
        if data['status'] == 'SL_HIT':
             logging.warning(f"🚨 ポジション決済 (SL) {symbol}: 価格 {data['price']} @ SL {ACTUAL_POSITIONS.get(symbol, {}).get('sl_price', 'N/A')}")
        else: # TP_HIT
             logging.info(f"🎉 ポジション決済 (TP) {symbol}: 価格 {data['price']} @ TP {ACTUAL_POSITIONS.get(symbol, {}).get('tp_price', 'N/A')}")
        
    # 必要に応じて時間ごとのステータス通知
    global LAST_HOURLY_NOTIFICATION_TIME
    if time.time() - LAST_HOURLY_NOTIFICATION_TIME > 60 * 60:
        await send_position_status_notification("📈 定期ステータス通知")
        LAST_HOURLY_NOTIFICATION_TIME = time.time()

async def send_position_status_notification(subject: str):
    """現在のポジションと残高のステータスをTelegramに送信"""
    balance = await fetch_current_balance_usdt()
    message = format_position_status_message(balance, ACTUAL_POSITIONS)
    send_telegram_html(message)


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
                # エラーやNoneの結果を除外
                if isinstance(result, Exception):
                    error_name = type(result).__name__
                    # logging.warning(f"分析タスク実行中に例外が発生: {error_name}: {result}")
                    continue
                if result:
                    all_signals.append(result)
            
            # 7. 最適なシグナルの選定と通知
            # ロングシグナルのみを対象
            long_signals = [s for s in all_signals if s['side'] == 'ロング' and s['score'] >= SIGNAL_THRESHOLD]
            
            # スコア降順、RRR降順でソート
            long_signals.sort(key=lambda s: (s['score'], s['rr_ratio']), reverse=True)
            
            # トップシグナルを選定
            top_signals_to_notify = []
            notified_count = 0
            
            for signal in long_signals:
                symbol = signal['symbol']
                current_time = time.time()
                
                # クールダウンチェック
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
            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.10)。次の分析まで {LOOP_INTERVAL} 秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            
            # 残高エラーは既にログ出力されているので、ここでは繰り返さない
            if error_name != 'Exception' or not str(e).startswith("残高取得エラー"):
                 logging.error(f"メインループで致命的なエラー: {error_name}: {e}")
            
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# (バージョン更新のみ)
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.10 - Extreme Balance Debug")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.10 Startup initializing (Extreme Balance Debug)...") 
    
    # CCXT初期化
    await initialize_ccxt_client()
    
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
        "bot_version": "v19.0.10 - Extreme Balance Debug",
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
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.10 - Extreme Balance Debug"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=os.environ.get("PORT", 8000))
