# ====================================================================================
# Apex BOT v20.0.37 - Future Trading / 30x Leverage 
# (Patch 81 & 80 & 73: CRITICAL FIX - NameError, Robust Lot Size, MEXC Error Cooldown)
#
# 改良・修正点:
# 1. 【バージョン更新】BOTバージョンを v20.0.37 に更新。
# 2. 【CRITICAL FIX: Patch 81】get_current_threshold 内で発生していた NameError を修正済み。
# 3. 【エラー処理強化: Patch 73】execute_trade_logic にて、MEXCの「流動性不足/Oversold (30005)」エラーを捕捉した場合、
#    その銘柄の取引を一時的にクールダウンさせ、短時間での無駄な再試行とレートリミットを回避するロジックを維持。
# 4. 【ロット修正: Patch 80】最小ロットをわずかに超える値を使用し、取引所の「greater than」制約を確実に回避するロジックを維持。
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
import math # 数値計算ライブラリ

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
# 🚨 注意: CCXTの標準シンボル形式 ('BTC/USDT') を使用
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
BOT_VERSION = "v20.0.37"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # 💡 FGI API URL

LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間間隔 (秒) - 10秒ごと

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 # 取引倍率
TRADE_TYPE = 'future' # 取引タイプ
MIN_MAINTENANCE_MARGIN_RATE = 0.005 # 最低維持証拠金率 (例: 0.5%) - 清算価格計算に使用

# 💡 レートリミット対策用定数 (Patch 68で導入)
LEVERAGE_SETTING_DELAY = 1.0 # レバレッジ設定時のAPIレートリミット対策用遅延 (秒)

# 💡 リスクベースの動的ポジションサイジング設定 
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    
MAX_RISK_PER_TRADE_PERCENT = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "0.01")) # 最大リスク: 総資産の1%

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
ACCOUNT_EQUITY_USDT: float = 0.0 # 現時点での総資産 (リスク計算に使用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 1 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  # 初期スコア
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ/一致時のボーナス
STRUCTURAL_PIVOT_BONUS = 0.05       # 構造的な優位性ボーナス (固定)
RSI_MOMENTUM_LOW = 40              # RSIモメンタム加速の閾値
MACD_CROSS_PENALTY = 0.15          # MACDモメンタム逆行時のペナルティ/一致時のボーナス
LIQUIDITY_BONUS_MAX = 0.06          # 流動性ボーナス
FGI_PROXY_BONUS_MAX = 0.05         # FGIマクロ要因最大影響度
FOREX_BONUS_MAX = 0.0               # 為替マクロ要因最大影響度 (未使用)

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"
        
def format_price(price: float) -> str:
    """価格を整形する"""
    if price is None:
        price = 0.0
    # 0.01より大きい場合は小数点以下2桁、それ以外は動的に
    if price >= 0.01:
        return f"{price:,.2f}"
    return f"{price:,.8f}".rstrip('0').rstrip('.')

# 清算価格の計算関数
def calculate_liquidation_price(entry_price: float, leverage: int, side: str = 'long', maintenance_margin_rate: float = MIN_MAINTENANCE_MARGIN_RATE) -> float:
    """
    指定されたエントリー価格、レバレッジ、維持証拠金率に基づき、
    推定清算価格 (Liquidation Price) を計算する。
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算
        liquidation_price = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)
    else:
        return 0.0
        
    return max(0.0, liquidation_price) # 価格は0未満にはならない

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

# 💡 CRITICAL FIX (Patch 81): get_current_threshold は閾値を返すのみ
def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        return SIGNAL_THRESHOLD_NORMAL

# 💡 NEW/RESTORED FUNCTION (Patch 81): format_startup_message
def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # 💡 current_threshold に応じてテキストを決定するロジック (Patch 81で分離)
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **リスクベースサイジング**\n" 
        f"  - **最大リスク/取引**: <code>{MAX_RISK_PER_TRADE_PERCENT*100:.2f}</code> %\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].split('/')[0] # /USDTを除去
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' 
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

    balance_section += f"\n"

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal.get('timeframe', '1h')
    score = signal['score']
    side = signal.get('side', 'long') 
    
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算をLong/Shortで反転
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)

    if context == "取引シグナル":
        lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = signal.get('notional_value', 0.0) # 名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount_raw = trade_result.get('filled_amount', 0.0)
            try:
                filled_amount = float(filled_amount_raw)
            except (ValueError, TypeError):
                filled_amount = 0.0
                
            filled_usdt_notional = trade_result.get('filled_usdt', 0.0) 
            risk_usdt = signal.get('risk_usdt', 0.0) # リスク額
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **リスク許容額**: <code>{format_usdt(risk_usdt)}</code> USDT ({MAX_RISK_PER_TRADE_PERCENT*100:.2f}%)\n" 
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **名目約定額**: <code>{format_usdt(filled_usdt_notional)}</code> USDT\n" 
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        try:
            filled_amount = float(filled_amount_raw)
            # PNL計算のための符号調整 (ショートの場合、filled_amountはマイナスだが、ここでは絶対値を使用)
            # filled_amount = abs(filled_amount) 
        except (ValueError, TypeError):
            filled_amount = 0.0
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_price(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_price(take_profit)}</code>\n"
        f"  - **清算価格 (Liq. Price)**: <code>{format_price(liquidation_price)}</code>\n" 
        f"  - **リスク幅 (SL)**: <code>{format_usdt(risk_width)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(reward_width)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val >= LONG_TERM_REVERSAL_PENALTY else "🟡 長期トレンド逆行"
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val >= MACD_CROSS_PENALTY else "🟡 MACDモメンタム逆行"
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val >= 0 else "🔴 FGIマクロ向かい風"
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")
    
    return "\n".join([f"    - {line}" for line in breakdown_list])

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), 
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        EXCHANGE_CLIENT = None
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        options = {
            'defaultType': 'future', 
        }

        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms 
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            
            symbols_to_set_leverage = []
            
            # --- 🚀 Patch 70 FIX: レバレッジ設定対象を監視銘柄リストに限定する ---
            
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                 # USDT建てのSwap/Future市場を探す
                 if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                     
                     # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                     if mkt['base'] in default_base_quotes:
                         # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                         symbols_to_set_leverage.append(mkt['symbol']) 
            
            # --- Patch 70 FIX 終了 ---

            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1} 
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                    
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY) 

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                    
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)


            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")


        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        if not balance:
            raise Exception("Balance object is empty.")

        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # 2. MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            
            if isinstance(raw_data, dict) and 'data' in raw_data:
                mexc_raw_data = raw_data.get('data')
            else:
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                mexc_data = mexc_raw_data
                
            if mexc_data:
                total_usdt_balance_fallback = 0.0
                
                if mexc_data.get('currency') == 'USDT':
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('currency') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                if total_usdt_balance_fallback > 0:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning("⚠️ MEXC専用フォールバックロジックで Equity を取得しました。")

        ACCOUNT_EQUITY_USDT = total_usdt_balance

        return {
            'total_usdt_balance': total_usdt_balance, 
            'open_positions': [], 
            'error': False
        }

    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_open_positions() -> List[Dict]:
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []
        
    try:
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions_ccxt = await EXCHANGE_CLIENT.fetch_positions()
        else:
            logging.error("❌ ポジション取得失敗: 取引所が fetch_positions APIをサポートしていません。")
            return []
            
        new_open_positions = []
        for p in positions_ccxt:
            if p and p.get('symbol') and p.get('contracts', 0) != 0:
                # ユーザーが監視対象としている銘柄のみを抽出 (シンボル形式が一致することを前提)
                if p['symbol'] in CURRENT_MONITOR_SYMBOLS:
                    side = 'short' if p['contracts'] < 0 else 'long'
                    
                    entry_price = p.get('entryPrice')
                    contracts = abs(p['contracts'])
                    notional_value = p.get('notional')
                    
                    if entry_price is None or notional_value is None:
                         logging.warning(f"⚠️ {p['symbol']} のポジション情報が不完全です。スキップします。")
                         continue
                    
                    new_open_positions.append({
                        'symbol': p['symbol'],
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': contracts,
                        'filled_usdt': notional_value, 
                        'timestamp': p.get('timestamp', time.time() * 1000),
                        'stop_loss': 0.0,
                        'take_profit': 0.0,
                    })

        OPEN_POSITIONS = new_open_positions
        
        # ログ強化ポイント: ポジション数が0の場合のログをより明示的に
        if len(OPEN_POSITIONS) == 0:
            logging.info("✅ CCXTから最新のオープンポジション情報を取得しました (現在 0 銘柄)。 **(ポジション不在)**")
        else:
            logging.info(f"✅ CCXTから最新のオープンポジション情報を取得しました (現在 {len(OPEN_POSITIONS)} 銘柄)。")
            
        return OPEN_POSITIONS

    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ ポジション取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ ポジション取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return []


# ====================================================================================
# ANALYTICAL CORE 
# ====================================================================================

def _calculate_ta_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたOHLCV DataFrameにテクニカル指標を適用する。
    """
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        return df
        
    # 指標の計算
    df[f'SMA{LONG_TERM_SMA_LENGTH}'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['RSI'] = ta.rsi(df['close'], length=14)
    # MACD (デフォルト設定: 12, 26, 9)
    macd_data = ta.macd(df['close'])
    df['MACD'] = macd_data['MACD_12_26_9']
    df['MACDh'] = macd_data['MACDh_12_26_9'] # ヒストグラム
    
    # ATR
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # OBV
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA20'] = ta.sma(df['OBV'], length=20)
    
    # PPO (Percentage Price Oscillator)のMACD (ボラティリティ測定用)
    # PPO_HIST は MACDh_12_26_9と似た挙動をするため、今回は簡略化し、MACDhをそのまま使用。
    # 代わりに、ボリンジャーバンドの幅の代わりとなるボラティリティ指標として、MACDhの絶対値を採用。
    df['PPO_HIST'] = df['MACDh'] # 簡易的なボラティリティ指標として使用
    
    return df

async def calculate_fgi() -> Dict:
    try:
        # 💡 FGI_API_URL を使用
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        # response.json() は {"data": [{...}], "metadata": {...}} の形式を想定
        data = response.json().get('data') 
        
        fgi_raw_value = int(data[0]['value']) if data and data[0]['value'] else 50
        fgi_classification = data[0]['value_classification'] if data and data[0]['value_classification'] else "Neutral"
        
        # FGIをスコアに変換: 0-100 -> -1.0 to 1.0 (例: 100=Greed=1.0, 0=Fear=-1.0)
        fgi_proxy = (fgi_raw_value / 50.0) - 1.0 
        
        return {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': f"{fgi_raw_value} ({fgi_classification})",
            'forex_bonus': 0.0 
        }
    except Exception as e:
        logging.error(f"❌ FGIの取得に失敗しました: {e}")
        return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A (APIエラー)', 'forex_bonus': 0.0}

async def get_top_volume_symbols(exchange: ccxt_async.Exchange, limit: int = TOP_SYMBOL_LIMIT, base_symbols: List[str] = DEFAULT_SYMBOLS) -> List[str]:
    """
    取引所から出来高トップの先物銘柄を動的に取得し、基本リストに追加する
    """
    
    logging.info(f"🔄 出来高トップ {limit} 銘柄の動的取得を開始します...")
    
    try:
        # 1. 全ティッカー情報（価格、出来高など）を取得
        tickers = await exchange.fetch_tickers()
        
        # 'NoneType' object has no attribute 'keys' のエラー対策 (Patch 75 Fix)
        if tickers is None or not isinstance(tickers, dict):
            logging.error(f"❌ {exchange.id}: fetch_tickersがNoneまたは無効なデータを返しました。デフォルトを使用します。")
            return base_symbols 

        volume_data = []
        
        for symbol, ticker in tickers.items():
            market = exchange.markets.get(symbol)
            
            # 1. 市場情報が存在し、アクティブであること
            if market is None or not market.get('active'):
                 continue

            # 2. Quote通貨がUSDTであり、取引タイプが先物/スワップであること (USDT-margined futures)
            if market.get('quote') == 'USDT' and market.get('type') in ['swap', 'future']:
                
                # 'quoteVolume' (引用通貨建て出来高 - USDT) を優先的に使用
                volume = ticker.get('quoteVolume')
                if volume is None:
                    # quoteVolumeがない場合、baseVolumeと最終価格で計算（概算）
                    base_vol = ticker.get('baseVolume')
                    last_price = ticker.get('last')
                    if base_vol is not None and last_price is not None:
                        volume = base_vol * last_price
                
                if volume is not None and volume > 0:
                    volume_data.append((symbol, volume))
        
        # 3. 出来高で降順にソートし、TOP N（40）のシンボルを抽出
        volume_data.sort(key=lambda x: x[1], reverse=True)
        top_symbols = [s for s, v in volume_data[:limit]]
        
        # 4. デフォルトリストと結合し、重複を排除（動的取得できなかった場合も主要銘柄は維持）
        # 優先度の高いデフォルト銘柄を先頭に、出来高トップ銘柄を追加する形でリストを作成
        unique_symbols = list(base_symbols)
        for symbol in top_symbols:
            if symbol not in unique_symbols:
                unique_symbols.append(symbol)
        
        logging.info(f"✅ 出来高トップ銘柄を動的に取得しました (合計 {len(unique_symbols)} 銘柄)。")
        return unique_symbols

    except Exception as e:
        # エラーが発生した場合、デフォルトリストのみを返す (耐障害性の維持)
        # 💥 FIX: CCXT内部で発生したAttributeErrorのトレーサックを抑制 (exc_info=False)
        logging.error(f"❌ 出来高トップ銘柄の取得に失敗しました。デフォルト ({len(base_symbols)}件) を使用します: {e}", exc_info=False)
        return base_symbols

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    try:
        # ccxt.fetch_ohlcv を使用
        ohlcv_data = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # DataFrameに変換
        ohlcv = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        ohlcv['timestamp'] = pd.to_datetime(ohlcv['timestamp'], unit='ms')
        
        if ohlcv.empty:
            raise Exception("OHLCV data is empty.")
            
        return ohlcv
        
    except Exception as e:
        logging.warning(f"⚠️ {symbol} {timeframe}: OHLCVデータの取得に失敗しました: {e}")
        return None

def apply_technical_analysis(symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Dict:
    """
    指定されたOHLCV DataFrameにテクニカル指標を適用し、分析結果を返す。
    """
    analyzed_data: Dict[str, Dict] = {}
    
    # 各時間足に指標を適用
    for tf, df in ohlcv.items():
        # SMA200の計算に必要な期間（約200期間）を確保できない場合はスキップ
        if not df.empty and len(df) >= LONG_TERM_SMA_LENGTH:
            analyzed_df = _calculate_ta_indicators(df)
            
            # 必須カラムのチェック (KeyError対策)
            required_cols = [f'SMA{LONG_TERM_SMA_LENGTH}', 'RSI', 'MACDh', 'ATR', 'OBV', 'OBV_SMA20']
            if not all(col in analyzed_df.columns for col in required_cols):
                 logging.warning(f"⚠️ {symbol} {tf}: 必須TA指標の計算に失敗しました (データ不足か計算エラー)。スキップ。")
                 continue
                 
            # 最終的な分析結果を抽出
            last = analyzed_df.iloc[-1] 
            
            # NaNチェック (NaNがあればその時間足の分析は無効)
            if any(pd.isna(last[col]) for col in required_cols):
                logging.warning(f"⚠️ {symbol} {tf}: 最新のTA結果にNaNが含まれています。スキップ。")
                continue
            
            # OBVの簡易トレンド: OBVがOBVのSMAを上回っているか
            obv_up = last['OBV'] > last['OBV_SMA20']
            
            analyzed_data[tf] = {
                'close': last['close'],
                'sma200': last[f'SMA{LONG_TERM_SMA_LENGTH}'],
                'rsi': last['RSI'],
                'macd_h': last['MACDh'],
                'atr': last['ATR'],
                'ppo_hist': last['MACDh'], # 簡易的なボラティリティ指標
                'obv_up': obv_up,
                'is_bull_trend': last['close'] > last[f'SMA{LONG_TERM_SMA_LENGTH}'],
                'is_bear_trend': last['close'] < last[f'SMA{LONG_TERM_SMA_LENGTH}'],
            }
        
    return analyzed_data

def calculate_signal_score(symbol: str, tech_signals: Dict, macro_context: Dict) -> Dict:
    """
    テクニカル分析の結果を統合し、最終的な複合シグナルスコアとSL/TP比率を計算する。
    """
    
    # メインの取引時間足と長期トレンド確認時間足
    main_tf = '1h'
    long_tf = '4h'
    
    if main_tf not in tech_signals or long_tf not in tech_signals:
        logging.warning(f"⚠️ {symbol}: 必要な時間足 ({main_tf}または{long_tf}) の分析データが不足しています。スコア0.0。")
        return {'score': 0.0, 'side': 'none', 'sl_ratio': 0.0, 'tp_ratio': 0.0, 'rr_ratio': 0.0, 'tech_data': {}}
        
    main_sig = tech_signals[main_tf]
    long_sig = tech_signals[long_tf]
    
    # 1. シグナル方向の決定 (ロング/ショート)
    # (4hトレンド + 1h MACD + 1h RSI) の賛成票で決定
    
    long_bias_score = 0
    short_bias_score = 0
    
    # 長期トレンド (4h SMA200)
    if long_sig['is_bull_trend']: long_bias_score += 1
    if long_sig['is_bear_trend']: short_bias_score += 1
        
    # メイン時間足のMACDモメンタム (ヒストグラムがプラス/マイナス)
    if main_sig['macd_h'] > 0: long_bias_score += 1
    if main_sig['macd_h'] < 0: short_bias_score += 1
        
    # メイン時間足のRSI (過熱感を避け、50付近でモメンタムを確認)
    if main_sig['rsi'] > 55 and main_sig['rsi'] < 70: long_bias_score += 1
    if main_sig['rsi'] < 45 and main_sig['rsi'] > 30: short_bias_score += 1
        
    if long_bias_score > short_bias_score:
        side = 'long'
    elif short_bias_score > long_bias_score:
        side = 'short'
    else:
        return {'score': 0.0, 'side': 'none', 'sl_ratio': 0.0, 'tp_ratio': 0.0, 'rr_ratio': 0.0, 'tech_data': {}}
        
    # 2. スコアリングの実行
    score = BASE_SCORE # 0.40 から開始
    tech_data = {
        'long_term_reversal_penalty_value': 0.0,
        'structural_pivot_bonus': 0.0,
        'macd_penalty_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'forex_bonus': 0.0, 
        'volatility_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0
    }
    
    # A. 長期トレンド一致の確認 (4h SMA200)
    trend_consistent = (side == 'long' and long_sig['is_bull_trend']) or \
                       (side == 'short' and long_sig['is_bear_trend'])
    if not trend_consistent:
        score -= LONG_TERM_REVERSAL_PENALTY
        tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY
    else:
        # トレンド一致の場合はボーナスを計上
        score += LONG_TERM_REVERSAL_PENALTY 
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
            
    # B. MACD/モメンタムの確認 (1h MACDヒストグラム)
    macd_consistent = (side == 'long' and main_sig['macd_h'] > 0) or \
                      (side == 'short' and main_sig['macd_h'] < 0)
    if not macd_consistent:
        score -= MACD_CROSS_PENALTY
        tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY
    else:
        # モメンタム一致の場合はボーナスを計上
        score += MACD_CROSS_PENALTY 
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
        
    # C. RSI モメンタム加速 (1h RSI) - 勢いがある場合
    # RSI_MOMENTUM_LOW (40) を使用し、40-60レンジからの離脱を確認
    rsi_favorable = (side == 'long' and main_sig['rsi'] > 60) or \
                    (side == 'short' and main_sig['rsi'] < 40)
    if rsi_favorable:
        bonus = 0.05
        score += bonus
        tech_data['rsi_momentum_bonus_value'] = bonus
        
    # D. 出来高/OBV確証 (1h OBV)
    obv_matches = (side == 'long' and main_sig['obv_up']) or \
                  (side == 'short' and not main_sig['obv_up'])
    if obv_matches:
        score += OBV_MOMENTUM_BONUS
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        
    # E. 流動性ボーナス (出来高トップ銘柄であるため、常に適用)
    score += LIQUIDITY_BONUS_MAX
    tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX
    
    # F. FGIマクロ影響
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_bonus_raw = fgi_proxy * FGI_PROXY_BONUS_MAX
    # LongはFGIがプラスだとボーナス、ShortはFGIがマイナスだとボーナス
    fgi_effective_bonus = fgi_bonus_raw if side == 'long' else -fgi_bonus_raw 
    score += fgi_effective_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_effective_bonus
    tech_data['forex_bonus'] = macro_context.get('forex_bonus', 0.0) # 0.0
    
    # G. ボラティリティ過熱ペナルティ (MACDhの絶対値が極端に大きい場合)
    if abs(main_sig['ppo_hist']) > (main_sig['close'] * VOLATILITY_BB_PENALTY_THRESHOLD / 100): # 終値の0.01%を簡易閾値
         vol_penalty = -0.10
         score += vol_penalty
         tech_data['volatility_penalty_value'] = vol_penalty
         
    # H. 構造的ピボットボーナス (常に適用 - 簡易的な優位性)
    score += STRUCTURAL_PIVOT_BONUS 
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # スコアを0.0から1.0の間に正規化
    final_score = max(0.0, min(1.0, score))
    
    # 3. SL/TP比率の計算 (ATRベース)
    
    current_atr = main_sig['atr']
    current_price = main_sig['close']
    
    # SL幅をATRのATR_MULTIPLIER_SL倍に設定
    sl_price_deviation = current_atr * ATR_MULTIPLIER_SL 
    sl_ratio = sl_price_deviation / current_price
    
    # 最小リスクパーセンテージの保証
    sl_ratio = max(sl_ratio, MIN_RISK_PERCENT)
        
    # TP比率とRRRの決定
    # スコアが高いほどRRRを上げる (最低2.0から開始)
    target_rr_base = 2.0 
    
    # スコアが SIGNAL_THRESHOLD (0.65) を超えた場合に、さらにボーナスを加算
    # (スコア - 閾値) * 4.0 を追加のRRRボーナスとする
    rr_adjustment = max(0.0, (final_score - SIGNAL_THRESHOLD) * 4.0) 
    rr_ratio = max(target_rr_base, target_rr_base + rr_adjustment)
    
    tp_ratio = sl_ratio * rr_ratio
    
    # 4. 結果の構築
    result = {
        'score': final_score, 
        'side': side, 
        'sl_ratio': sl_ratio, 
        'tp_ratio': tp_ratio, 
        'rr_ratio': rr_ratio, 
        'tech_data': tech_data,
        'timeframe': main_tf # 使用した時間足を明記
    }
    
    return result

async def execute_trade_logic(signal: Dict) -> Optional[Dict]:
    """
    取引実行ロジック。
    ロットサイズ計算を強化し、取引所の精度で最小ロットを保証する。
    """
    global LAST_SIGNAL_TIME
    
    if TEST_MODE:
        # TEST_MODEでは、計算結果のみを返す
        max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
        sl_ratio = abs(signal['entry_price'] - signal['stop_loss']) / signal['entry_price']
        notional_value_usdt_calculated = max_risk_usdt / sl_ratio
        
        return {'status': 'skip', 'error_message': 'TEST_MODE is ON', 'filled_usdt': notional_value_usdt_calculated}
    
    if not ACCOUNT_EQUITY_USDT or ACCOUNT_EQUITY_USDT <= 0:
        return {'status': 'error', 'error_message': 'Account equity is zero or not fetched.'}
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    
    # 1. リスクベースのロットサイズ計算
    max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
    price_diff_to_sl = abs(entry_price - stop_loss)
    
    if price_diff_to_sl <= 0:
        return {'status': 'error', 'error_message': 'Stop Loss is too close or invalid.'}
        
    sl_ratio = abs(entry_price - stop_loss) / entry_price
    notional_value_usdt_calculated = max_risk_usdt / sl_ratio
    lot_size_units_calculated = notional_value_usdt_calculated / entry_price 

    # 2. 💡 Patch 72 FIX: 最小取引ロットの精度チェックを強化
    market_info = EXCHANGE_CLIENT.markets[symbol]
    min_amount_raw = market_info.get('limits', {}).get('amount', {}).get('min', 0.0001)

    # 最小取引サイズを、取引所の精度（ステップサイズ）で調整し、「実際に取引可能な最小ロット」を特定する。
    try:
        # ⚠️ WARN: MEXCはここでエラーを出すことがあるため、try-exceptで捕捉し、raw値で続行する
        min_amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, min_amount_raw)
        min_amount_adjusted = float(min_amount_adjusted_str)
    except Exception as e:
        # ログに記録されたエラー「mexc amount of SAND/USDT must be greater than minimum amount precision of 0.01. raw:0.00010000」に対応
        logging.error(f"❌ {symbol}: 最小ロットの精度調整に失敗しました: {e}. raw:{min_amount_raw:.8f}", exc_info=False)
        # 精度調整に失敗した場合、CCXTが取得した最小ロット値をそのまま使用する（リスクあり）
        min_amount_adjusted = min_amount_raw

    # 万一、precision adjustmentで0になった場合の最終防衛
    if min_amount_adjusted <= 0.0:
        logging.error(f"❌ {symbol}: min_amount_raw ({min_amount_raw:.8f}) を precision 調整した結果、0以下になりました。取引を停止します。")
        return {'status': 'error', 'error_message': 'Precision adjustment makes min_amount zero or less.'}

    # 3. 最終的に使用するロットサイズを決定
    
    # 💥 FIX (Patch 80): 最小ロットと同値でのエラー回避のため、計算ロットが最小ロットを下回る場合は、
    # 最小ロット * わずかに大きな値 (1.00001) を使用し、'greater than' の条件を満たすようにする。
    if lot_size_units_calculated < min_amount_adjusted:
         lot_size_units = min_amount_adjusted * 1.00001 # わずかに増やす
         logging.warning(f"⚠️ {symbol}: 計算ロット ({lot_size_units_calculated:.8f}) が最小ロット ({min_amount_adjusted:.8f}) を下回ったため、最小ロットをわずかに超える値を使用します。")
    else:
         lot_size_units = lot_size_units_calculated
         
    notional_value_usdt = lot_size_units * entry_price # 概算の名目価値

    # 4. 注文の実行
    try:
        side_ccxt = 'buy' if side == 'long' else 'sell'
        
        # 契約数量を取引所の精度に合わせて最終調整
        amount_adjusted_str = EXCHANGE_CLIENT.amount_to_precision(symbol, lot_size_units)
        amount_adjusted = float(amount_adjusted_str)
        
        # ログ強化: 注文を出す直前の最終ロットサイズと最小ロットを記録
        logging.info(f"✅ {symbol}: 最終ロットサイズ {amount_adjusted:.8f} (最小ロット: {min_amount_adjusted:.8f} / 計算ベース: {lot_size_units_calculated:.8f})")
        
        # 最終チェック: 精度調整の結果、最小取引ロットを下回った場合、強制的に最小ロットに戻す
        # 💥 Patch 80で *1.00001 を使用しているため、ここでは比較のみで十分（理論上は不要だが安全策として残す）
        if amount_adjusted < min_amount_adjusted:
             amount_adjusted = min_amount_adjusted
             logging.warning(f"⚠️ {symbol}: 精度調整後の数量 ({amount_adjusted_str}) が最小ロット ({min_amount_adjusted:.8f}) を下回ったため、最小ロットを再採用しました。")
        
        if amount_adjusted <= 0.0:
             # このエラーはamount_to_precision後のamount_adjustedが0以下になった場合に発生
             logging.error(f"❌ {symbol} 注文実行エラー: amount_to_precision後の数量 ({amount_adjusted:.8f}) が0以下になりました。")
             return {'status': 'error', 'error_message': 'Amount rounded down to zero by precision adjustment.'}
        
        # 注文実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            type='market',
            side=side_ccxt,
            amount=amount_adjusted,
            params={}
        )

        # 5. ポジション情報を更新
        new_position = {
            'symbol': symbol,
            'side': side,
            'entry_price': order['price'] if order['price'] else entry_price, 
            'contracts': amount_adjusted if side == 'long' else -amount_adjusted,
            'filled_usdt': notional_value_usdt, 
            'timestamp': time.time() * 1000,
            'stop_loss': stop_loss,
            'take_profit': signal['take_profit'],
        }
        OPEN_POSITIONS.append(new_position)
        
        # クールダウンタイマーをセット
        LAST_SIGNAL_TIME[symbol] = time.time()
        
        return {
            'status': 'ok',
            'filled_amount': amount_adjusted,
            'filled_usdt': notional_value_usdt,
            'entry_price': new_position['entry_price'],
            'stop_loss': stop_loss,
            'take_profit': signal['take_profit'],
            'exit_type': 'N/A'
        }
        
    except ccxt.ExchangeError as e:
        error_message = e.args[0]
        
        # 💡 【Patch 73】MEXC: 流動性不足/Oversold (30005) のエラー処理を強化
        if 'code":30005' in error_message or 'Oversold' in error_message:
            
            # 1. ログ記録
            detail_msg = "MEXC: 流動性不足/Oversold (30005) により注文が拒否されました。この銘柄の取引を一時的にクールダウンさせます。"
            logging.error(f"❌ {symbol} 注文実行エラー: {detail_msg}")
            
            # 2. クールダウンタイマーをセット
            # シグナルが拒否された場合、次のシグナルをすぐに再実行しないように、クールダウン時間 (TRADE_SIGNAL_COOLDOWN) を設定します。
            LAST_SIGNAL_TIME[symbol] = time.time()
            
            # 3. エラー情報を返却
            return {'status': 'error', 'error_message': detail_msg}

        elif 'Amount can not be less than zero' in error_message or 'code":400' in error_message:
            # 💡 Patch 72/80でこのエラーの発生を大幅に抑えるロジックを実装済み
            detail_msg = f"MEXC: ロットサイズがゼロまたは小さすぎます (400)。(最終数量: {amount_adjusted:.8f})"
            logging.error(f"❌ {symbol} 注文実行エラー: {detail_msg} - ロット修正を試行しましたが失敗。")
            
            # 💡 400エラーの場合も、無限ループを防ぐためクールダウンさせる
            LAST_SIGNAL_TIME[symbol] = time.time()
            
            return {'status': 'error', 'error_message': detail_msg}
            
        else:
            logging.error(f"❌ {symbol} 注文実行エラー: {e}")
            return {'status': 'error', 'error_message': f"Exchange Error: {error_message}"}
            
    except Exception as e:
        logging.error(f"❌ {symbol} 注文実行中に予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f"Unexpected Error: {e}"}


# ====================================================================================
# SCHEDULERS & ENTRY POINT
# ====================================================================================

async def main_bot_loop():
    """メインの取引ロジックを格納する非同期関数"""
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_SIGNAL_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    LAST_SUCCESS_TIME = time.time()
    logging.info("⚙️ メインループを開始します。")

    try:
        # 0. グローバルコンテキストと口座情報の更新
        GLOBAL_MACRO_CONTEXT = await calculate_fgi()
        account_status = await fetch_account_status()
        
        if account_status.get('error'):
            logging.critical("❌ 口座情報の取得に失敗しました。取引をスキップします。")
            return

        # 1. 監視対象銘柄のリストを更新 (出来高ベース)
        CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols(EXCHANGE_CLIENT, TOP_SYMBOL_LIMIT, DEFAULT_SYMBOLS)
        await fetch_open_positions() # オープンポジション情報の更新
        
        # 2. 動的閾値の計算
        # 💡 NameError FIX (Patch 81): get_current_threshold は値を返すので、変数に代入して使用します。
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) 
        logging.info(f"📊 市場環境スコア: FGI {GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}。動的取引閾値: {current_threshold * 100:.2f} / 100")
        
        all_signals: List[Dict] = []
        
        # 3. 監視銘柄ごとの分析
        for symbol in CURRENT_MONITOR_SYMBOLS:
            
            cooldown_remaining = TRADE_SIGNAL_COOLDOWN - (time.time() - LAST_SIGNAL_TIME.get(symbol, 0))
            
            # ログ強化ポイント: シグナルスキップ理由のログ
            if cooldown_remaining > 0:
                logging.info(f"⏭️ {symbol}: スキップ (クールダウン中 - 残り {cooldown_remaining/3600:.1f} 時間)")
                continue
            
            # ログ強化ポイント: ポジション重複チェックのログ
            if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                logging.info(f"⏭️ {symbol}: スキップ (ポジション保有中 - ワンウェイモード)")
                continue
            
            # 3.1. OHLCVデータ取得 (全時間足)
            ohlcv_data: Dict[str, pd.DataFrame] = {}
            for tf in TARGET_TIMEFRAMES:
                df = await fetch_ohlcv_data(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
                if df is not None and not df.empty:
                    ohlcv_data[tf] = df
            
            if not ohlcv_data:
                continue

            # 3.2. テクニカルシグナル計算 
            tech_signals = apply_technical_analysis(symbol, ohlcv_data)
            
            # 3.3. 最終シグナルスコアとSL/TP計算
            signal = calculate_signal_score(symbol, tech_signals, GLOBAL_MACRO_CONTEXT)
            
            # 無効なシグナルをスキップ
            if signal['score'] <= 0.0:
                 continue
                 
            # 必須情報の追加
            signal['symbol'] = symbol
            
            all_signals.append(signal)

        # 4. シグナルフィルタリングと実行
        # スコア降順にソート
        top_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
        
        eligible_signals = []
        if top_signals:
            # 閾値を超えたシグナルのみを対象とする
            eligible_signals = [s for s in top_signals if s['score'] >= current_threshold]
            
            # ログ強化ポイント: 閾値を超えたシグナルの数を記録
            logging.info(f"📈 検出シグナル: {len(all_signals)} 銘柄。取引閾値 ({current_threshold*100:.2f}) を超えたシグナル: {len(eligible_signals)} 銘柄。")
            
            # TOP_SIGNAL_COUNT (現在は1) のシグナルのみを処理
            for signal in eligible_signals[:TOP_SIGNAL_COUNT]:
                
                # 4.1. SL/TP/清算価格の計算
                ticker = await EXCHANGE_CLIENT.fetch_ticker(signal['symbol'])
                current_price = ticker['last']
                side = signal['side']
                
                signal['entry_price'] = current_price
                
                # SL/TP価格の決定 (sl_ratio/tp_ratioはcalculate_signal_scoreで計算済み)
                sl_price = current_price * (1 - signal['sl_ratio'] if side == 'long' else 1 + signal['sl_ratio'])
                tp_price = current_price * (1 + signal['tp_ratio'] if side == 'long' else 1 - signal['tp_ratio'])
                
                # 清算価格の推定
                liq_price = calculate_liquidation_price(
                    current_price, 
                    LEVERAGE, 
                    side, 
                    MIN_MAINTENANCE_MARGIN_RATE
                )
                
                signal['stop_loss'] = sl_price
                signal['take_profit'] = tp_price
                signal['liquidation_price'] = liq_price
                
                # リスク許容額の計算 (通知用)
                max_risk_usdt = ACCOUNT_EQUITY_USDT * MAX_RISK_PER_TRADE_PERCENT
                signal['risk_usdt'] = max_risk_usdt

                logging.info(f"🔥 強力なシグナル検出 (実行対象): {signal['symbol']} - {signal['side'].upper()} (Score: {signal['score']*100:.2f})")
                
                # 4.2. 取引の実行
                trade_result = await execute_trade_logic(signal)
                
                # 4.3. 通知とロギング
                log_signal(signal, "取引シグナル", trade_result)
                await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
        else:
            # ログ強化ポイント: 閾値を超えたシグナルがなかった場合のログ
            logging.info(f"🔍 該当銘柄なし: {len(all_signals)} 銘柄のシグナルスコアが取引閾値 ({current_threshold*100:.2f}) を下回りました。** (スコア不足)**")


        # 5. 初回完了通知
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # 💡 NameError FIX (Patch 81): format_startup_message 関数を呼び出します。
            await send_telegram_notification(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold))
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        # 6. WebShareデータの送信
        if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
            webshare_data = {
                'timestamp': datetime.now(JST).isoformat(),
                'version': BOT_VERSION, 
                'account_status': account_status,
                'open_positions': OPEN_POSITIONS,
                'macro_context': GLOBAL_MACRO_CONTEXT,
                'signals': eligible_signals if 'eligible_signals' in locals() else [] 
            }
            await send_webshare_update(webshare_data)
            LAST_WEBSHARE_UPLOAD_TIME = time.time()


    except Exception as e:
        logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
        await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: `{e}`")


async def position_management_loop_async():
    """TP/SLを監視し、決済注文を実行する非同期関数"""
    global OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return
        
    closed_positions_symbols = []
    
    tickers = {}
    try:
        if OPEN_POSITIONS:
            symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.warning(f"⚠️ ポジション監視中の価格取得失敗: {e}")
        return

    for pos in list(OPEN_POSITIONS):
        symbol = pos['symbol']
        
        if symbol not in tickers or 'last' not in tickers[symbol]:
            continue
            
        current_price = tickers[symbol]['last']
        
        action = None
        
        # 決済ロジック (TP/SL)
        if pos['side'] == 'long':
            if current_price >= pos['take_profit']:
                action = 'TP_SELL'
            elif current_price <= pos['stop_loss']:
                action = 'SL_SELL'
        elif pos['side'] == 'short':
            if current_price <= pos['take_profit']:
                action = 'TP_BUY'
            elif current_price >= pos['stop_loss']:
                action = 'SL_BUY'

        if action:
            logging.warning(f"🔔 {symbol}: {action}トリガー！価格 {format_price(current_price)}")
            
            # 決済注文のサイドを決定
            side_ccxt = 'sell' if pos['side'] == 'long' else 'buy'
            
            # PNLの計算 (ロギング/通知用)
            pnl_usdt_calc = abs(pos['filled_usdt']) * (current_price - pos['entry_price']) / pos['entry_price'] * (1 if pos['side'] == 'long' else -1)
            # PNL率の計算 (簡易的なレバレッジ考慮: PNL / 証拠金)
            pnl_rate_calc = pnl_usdt_calc / (abs(pos['filled_usdt']) / LEVERAGE) 
            
            trade_result: Dict = {
                'status': 'ok',
                'exit_type': action.split('_')[0],
                'entry_price': pos['entry_price'],
                'exit_price': current_price,
                'filled_amount': abs(pos['contracts']),
                'pnl_usdt': pnl_usdt_calc,
                'pnl_rate': pnl_rate_calc,
            }
            
            # 💥 実際の決済注文ロジックを有効化 (Patch 78) 💥
            try:
                # 決済注文（成行）
                close_order = await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    type='market', 
                    side=side_ccxt, 
                    amount=abs(pos['contracts']),
                    params={'reduceOnly': True} 
                )
                logging.info(f"✅ {symbol} 決済注文実行: {action.split('_')[0]} でポジションをクローズしました。注文ID: {close_order.get('id', 'N/A')}")
                
            except Exception as e:
                 # 決済注文が失敗した場合 (APIエラー、レートリミットなど)
                 logging.error(f"❌ {symbol} 決済注文失敗: {e}")
                 # 決済失敗の場合、システム管理上のポジションは削除せず（次のループで再試行させる）
                 continue 
                 
            # 決済成功としてリストから削除
            closed_positions_symbols.append(symbol)
            
            # 通知とロギング
            log_signal(pos, "ポジション決済", trade_result)
            mock_signal = pos.copy()
            mock_signal['score'] = 0.8 # 通知に必要なダミースコア
            mock_signal['rr_ratio'] = 2.0 # 通知に必要なダミーRRR
            await send_telegram_notification(format_telegram_message(mock_signal, "ポジション決済", MIN_RISK_PERCENT, trade_result, exit_type=action.split('_')[0]))

    # 決済されたポジションをリストから削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] not in closed_positions_symbols]


# Uvicorn/FastAPIのアプリケーションインスタンス
app = FastAPI(title="Apex BOT API")

@app.get("/status", include_in_schema=False)
async def read_root():
    """ヘルスチェック用のルート"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    if IS_CLIENT_READY and IS_FIRST_MAIN_LOOP_COMPLETED:
        status_code = 200
        message = f"Apex BOT Service is running and CCXT client is ready. ({now_jst} JST)"
    else:
        status_code = 200 
        message = f"Apex BOT Service is running (Client initializing: {now_jst} JST)."
        
    return JSONResponse(
        status_code=status_code,
        content={"status": message, "version": BOT_VERSION, "timestamp": datetime.now(JST).isoformat()}
    )


async def main_bot_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    global LAST_SUCCESS_TIME
    while True:
        # クライアントの再初期化を試行
        if not IS_CLIENT_READY:
            logging.info("(main_bot_scheduler) - クライアント未準備のため、初期化を試行します。")
            # 初期化に失敗した場合、5秒待機して再試行
            if not await initialize_exchange_client(): 
                await asyncio.sleep(5)
                continue

        current_time = time.time()
        
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) 


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {type(exc).__name__}", "detail": str(exc)},
    )


if __name__ == "__main__":
    # 環境変数PORTからポート番号を取得。なければ10000を使用
    port = int(os.environ.get("PORT", 10000))
    # Uvicornを起動
    # 'main_render:app' はファイル名と変数名に依存するため、適切なファイル名に修正してください。
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
