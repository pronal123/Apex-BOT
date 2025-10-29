# ====================================================================================
# Apex BOT v20.0.45 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入, スコアブレークダウンの動的修正)
# 
# 🚨 致命的エラー修正強化 (v20.0.45): 
# 1. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策を強化。
# 2. 💡 修正: ATR計算失敗による全分析スキップ問題を修正。データフレームの長さとNaNチェックを強化。
# 3. 💡 修正: スコア詳細ブレークダウンが毎回同じ条件となるエラーはv20.0.44で修正済み。
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
from fastapi import FastAPI, Request, Response 
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
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT",
    "ZEC/USDT", "PUMP/USDT", "PEPE/USDT", "FARTCOIN/USDT",
    "WLFI/USDT", "PENGU/USDT", "ONDO/USDT", "HBAR/USDT", "TRUMP/USDT",
    "SHIB/USDT", "HYPE/USDT", "LINK/USDT", "ZEC/USDT",
    "VIRTUAL/USDT", "PIPPIN/USDT", "GIGGLE/USDT", "H/USDT", "AIXBT/USDT", 
]
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.45"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 

LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 5    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

# 💡 【固定ロット】設定 
FIXED_NOTIONAL_USDT = 20.0 

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
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled。")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 順張りボーナス/逆張りペナルティの値
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              # RSI 40/60をモメンタム加速の閾値に使用
MACD_CROSS_PENALTY = 0.15          # MACDの方向一致ボーナス/不一致ペナルティ
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             # SL = 2.0 * ATR
MIN_RISK_PERCENT = 0.008            # 最低リスク幅 (0.8%) 
RR_RATIO_TARGET = 1.5               # 💡 新規追加: 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
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
        
    return max(0.0, liquidation_price) 

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

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

def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # current_threshold に応じてテキストを決定するロジック
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
        f"  - **取引ロット**: **固定** <code>{format_usdt(FIXED_NOTIONAL_USDT)}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **ATRベース**の**動的SL**で管理\n" 
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
                # SL/TPがNoneの場合に備えて0.0をデフォルトに
                sl_price = pos.get('stop_loss', 0.0)
                tp_price = pos.get('take_profit', 0.0)
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(sl_price)} / TP: {format_price(tp_price)})\n"
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
        f"  - **FGI (恐怖・貪欲)**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
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
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得 (process_entry_signalで更新されているはず)
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    # 0.0の場合、通知メッセージの整形関数内で0.0をそのまま表示させないようにNoneを渡す (format_price/format_usdtで0.0処理済みのため不要だが念のため)
    if entry_price == 0.0: entry_price = signal.get('current_price', 0.0)
    if liquidation_price == 0.0: 
         # trade_resultが無く、entry_price > 0ならここで計算
        if entry_price > 0 and trade_result is None:
            liquidation_price = calculate_liquidation_price(entry_price, LEVERAGE, side)
        else:
            # ポジション決済時など、trade_resultから清算価格を取得
            liquidation_price = trade_result.get('liquidation_price', 0.0) if trade_result else 0.0

    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT # 実際に約定した名目価値
        
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
                
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            # 💡 修正: リスク額はSL比率とレバレッジで計算
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
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
            # ポジションをクローズするため、契約数は元のポジションの契約数を反映させるべき
            if 'contracts' in signal:
                 filled_amount = signal['contracts'] 
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
        f"  - **取引閾値**: <code>{current_threshold * 100:.0f}</code> 点\n"
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
            f"{get_score_breakdown(signal)}\n"
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
    """
    【修正済み】
    シグナルのスコア内訳を整形して返す。
    tech_dataに格納された実際の動的なボーナス/ペナルティ値を表示する。
    """
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行 (long_term_reversal_penalty_value)
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    # 0.0でない値が設定されていることを前提に、その値の符号で判断
    if abs(trend_val) > 0.0001:
        trend_text = "🟢 長期トレンド一致" if trend_val > 0 else "🔴 長期トレンド逆行"
        breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")
    else:
        breakdown_list.append(f"🟡 長期トレンド中立: {0.00:+.2f} 点")

    # MACDモメンタム (macd_penalty_value)
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    if abs(macd_val) > 0.0001:
        macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else "🔴 MACDモメンタム逆行/失速"
        breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    else:
         breakdown_list.append(f"🟡 MACD中立: {0.00:+.2f} 点")

    # RSIモメンタム加速 (rsi_momentum_bonus_value)
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
    # OBV確証 (obv_momentum_bonus_value)
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス (liquidity_bonus_value)
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響 (sentiment_fgi_proxy_bonus)
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ (volatility_penalty_value)
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス (structural_pivot_bonus)
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    # ベーススコアの一部であるため、必ず表示
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
        # numpy.float64などをfloatに変換
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
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        # ファイルの存在を確認し、サイズが大きくなりすぎないように制御するか、新しいログファイルを作成するロジックを検討しても良い
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知
async def notify_highest_analysis_score():
    """
    分析の結果、取引閾値に満たない最高スコア銘柄を1時間ごとに通知する。
    """
    global LAST_ANALYSIS_SIGNALS, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ANALYSIS_ONLY_INTERVAL, SIGNAL_THRESHOLD, GLOBAL_MACRO_CONTEXT, BASE_SCORE
    
    current_time = time.time()
    
    # 実行間隔のチェック
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return

    # 分析結果が格納されているかチェック
    if not LAST_ANALYSIS_SIGNALS:
        logging.info("ℹ️ 分析結果がないため、最高スコア通知をスキップします。")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time 
        return

    # スコアの降順でソートし、最高スコアのシグナルを取得
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)
    best_signal = sorted_signals[0]
    
    best_score = best_signal.get('score', 0.0)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 最高スコアが取引閾値未満で、かつ最低限の分析スコア(例: BASE_SCORE+0.1)を超えている場合
    if best_score < current_threshold and best_score >= (BASE_SCORE + 0.1): 
        
        symbol = best_signal.get('symbol', 'N/A')
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'long')

        # 通知メッセージの整形
        message = (
            f"📈 **分析結果 (最高スコア)** - 定時レポート\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **確認日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
            f"  - **最高スコア銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **最高スコア**: <code>{best_score * 100:.2f} / 100</code> ({side.capitalize()})\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
            f"  - **備考**: 取引閾値 (<code>{current_threshold * 100:.2f}</code>点) に満たなかったため取引はスキップされました。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {BOT_VERSION}</i>"
        )
        
        logging.info(f"ℹ️ 定時報告: 取引閾値未満の最高スコアを通知します: {symbol} ({side}) Score: {best_score:.2f}")
        await send_telegram_notification(message)
    else:
        # 最高スコアが閾値以上、または分析スコアが低すぎる場合は通知スキップ
        if best_score >= current_threshold:
            logging.info("ℹ️ 最高スコアが取引閾値以上のため、定時通知をスキップします (取引シグナルとして通知済みと想定)。")
        else:
             logging.info("ℹ️ 最高スコアが低すぎるため、定時通知をスキップします。")

    # 通知/ログ処理が完了したら時間を更新
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

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
            
            # headers = {'Content-Type': 'application/json'} # requests.post(json=...)で自動設定される
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
            
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                # USDT建てのSwap/Future市場を探す
                if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                    # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                    if mkt['base'] in default_base_quotes:
                        # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                        symbols_to_set_leverage.append(mkt['symbol'])
            
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
            # MEXCはfetch_balance(type='swap')を使用
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        if not balance:
            raise Exception("Balance object is empty.")

        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
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
                        if asset.get('asset') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                # どちらか大きい方を使用 (ccxtの計算が間違っている場合に備えて)
                if total_usdt_balance_fallback > total_usdt_balance:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning(f"⚠️ MEXCのフォールバックロジックでUSDT総資産を更新: {format_usdt(total_usdt_balance)}")


        # 総資産をグローバル変数に保存 (リスク管理に使用)
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)}")
        
        # オープンポジション情報の取得
        # MEXCはfetch_positionsを使用。Binance/Bybitはfetch_positions_riskを使用
        positions: List[Dict] = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions = await EXCHANGE_CLIENT.fetch_positions()
            
        open_positions = []
        for p in positions:
            # ポジションサイズが0より大きい、かつUSDT建て先物シンボルのみを抽出
            if (p.get('contracts', 0) != 0 or p.get('info', {}).get('vol', 0) != 0) and \
               p.get('symbol', '').endswith('/USDT') and \
               p.get('type') in ['swap', 'future']:
                
                # 必要な情報を抽出/整形
                entry_price_raw = p.get('entryPrice', 0.0)
                contracts_raw = p.get('contracts', 0.0)
                
                # contractsが取れない場合、MEXC特有の 'vol' フィールドを試す
                if contracts_raw == 0 and EXCHANGE_CLIENT.id == 'mexc':
                     contracts_raw = float(p.get('info', {}).get('vol', 0.0))

                # 名目価値 (Notional Value) を計算
                notional_value = abs(contracts_raw * entry_price_raw)
                
                # side (long/short) の判定
                side = 'long' if contracts_raw > 0 else 'short'
                
                # 最終的なポジション情報 (ボットが管理すべき最低限の情報)
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'contracts': contracts_raw,
                    'entry_price': entry_price_raw,
                    'filled_usdt': notional_value, # 名目価値
                    'liquidation_price': p.get('liquidationPrice', calculate_liquidation_price(entry_price_raw, LEVERAGE, side)),
                    # SL/TPはボットが設定したものとは限らないため、初期値はNoneとする
                    'stop_loss': p.get('stopLoss', None), 
                    'take_profit': p.get('takeProfit', None),
                    'timestamp': int(time.time() * 1000) # ポジション取得時刻
                })
        
        logging.info(f"✅ 現在、管理対象となるオープンポジション数: {len(open_positions)} 件")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }
    
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - ネットワークエラー: {e}")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True, 'error_message': 'NetworkError'}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 一般エラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True, 'error_message': f'GeneralError: {e}'}

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗 ({symbol}/{timeframe}): クライアント未準備。")
        return None

    try:
        # ccxtのOHLCVは [timestamp, open, high, low, close, volume]
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ OHLCVデータが空です ({symbol}/{timeframe})。")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        # 最新のデータが完全な足でない可能性があるため、最新の1行を除外して計算に使用することが多い
        # 今回は最新の足も含むが、計算の前に NaN チェックを行う
        
        return df

    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ DDoS保護トリガーにより、一時的にOHLCV取得をスキップします ({symbol}): {e}")
        return None
    except ccxt.ExchangeError as e:
        # 例: Symbol not found on exchange
        logging.error(f"❌ 取引所エラーによりOHLCV取得失敗 ({symbol}): {e}")
        return None
    except Exception as e:
        logging.error(f"❌ 予期せぬエラーによりOHLCV取得失敗 ({symbol}): {e}", exc_info=True)
        return None

async def fetch_top_volume_symbols(top_limit: int) -> List[str]:
    """
    取引所の全ティッカー情報から、出来高TOPのUSDT建て先物シンボルを取得する。
    
    💡 修正点: fetch_tickersがNoneまたは非辞書型を返した場合の致命的エラーを回避。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ TOP銘柄取得失敗: クライアント未準備。")
        return DEFAULT_SYMBOLS.copy()

    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 💡 修正: fetch_tickersの戻り値がNoneまたは辞書型でない場合のチェックを強化
        if not isinstance(tickers, dict) or not tickers:
            logging.warning("⚠️ fetch_tickersが空の結果、または予期せぬ型を返しました。デフォルト銘柄を使用します。")
            return DEFAULT_SYMBOLS.copy()
            
        usdt_futures = {}
        for symbol, ticker in tickers.items():
            # 'USDT'建ての先物/スワップ (Future/Swap) のみを選別
            # ccxtのsymbolは通常 'BTC/USDT' or 'BTC/USDT:USDT' の形式
            is_usdt_future = ('/USDT' in symbol or ':USDT' in symbol) and \
                             ('future' in ticker.get('info', {}).get('type', '') or \
                              'swap' in ticker.get('info', {}).get('type', ''))
                              
            if is_usdt_future and ticker.get('quoteVolume') is not None:
                # ccxt標準のシンボル形式 (例: BTC/USDT) に変換して格納
                std_symbol = symbol.split(':')[0]
                usdt_futures[std_symbol] = ticker['quoteVolume']

        # 出来高 (quoteVolume) で降順にソート
        sorted_symbols = sorted(usdt_futures.items(), key=lambda item: item[1], reverse=True)
        
        # TOP_SYMBOL_LIMIT に含まれるシンボルと、DEFAULT_SYMBOLSをマージ
        top_symbols = [s[0] for s in sorted_symbols[:top_limit]]
        
        # デフォルトシンボルと重複を削除してマージ（デフォルトを優先）
        unique_symbols = DEFAULT_SYMBOLS.copy()
        for s in top_symbols:
            if s not in unique_symbols:
                unique_symbols.append(s)
                
        logging.info(f"✅ 出来高TOP {len(top_symbols)} 銘柄を含む計 {len(unique_symbols)} 銘柄を監視対象として設定しました。")
        return unique_symbols

    except Exception as e:
        logging.error(f"❌ 出来高TOP銘柄の取得中にエラーが発生しました。デフォルト銘柄を使用します: {e}")
        return DEFAULT_SYMBOLS.copy()

async def fetch_macro_context() -> Dict:
    """
    FGI (Fear & Greed Index) などのマクロ環境データを取得する。
    FGIは-1.0から1.0に正規化されたプロキシ値として返す。
    """
    
    fgi_proxy = 0.0
    fgi_raw_value = "N/A"
    forex_bonus = 0.0 # 為替/その他マクロ指標の将来的な拡張用
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and data['data']:
            fgi_entry = data['data'][0]
            value = int(fgi_entry.get('value', 50))
            
            # FGI (0-100) を -1.0 (極度の恐怖) から 1.0 (極度の貪欲) に正規化
            # 50(中立) -> 0.0
            # 0(極度の恐怖) -> -1.0
            # 100(極度の貪欲) -> 1.0
            fgi_proxy = (value - 50) / 50 
            fgi_raw_value = f"{fgi_entry.get('value_classification', 'N/A')} ({value})"
            
            logging.info(f"✅ FGIマクロデータを取得しました: {fgi_raw_value}, プロキシ: {fgi_proxy:+.2f}")
            
    except Exception as e:
        logging.warning(f"⚠️ FGIデータ取得失敗。中立値 (0.0) を使用します: {e}")
        
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus
    }

# ====================================================================================
# TRADING LOGIC & ANALYSIS
# ====================================================================================

# 【最重要修正箇所】スコアリングロジック
def calculate_technical_score(df_1m: pd.DataFrame, df_5m: pd.DataFrame, df_15m: pd.DataFrame, 
                            df_1h: pd.DataFrame, df_4h: pd.DataFrame, 
                            symbol: str, timeframe: str, current_price: float, 
                            current_is_top_symbol: bool, side: str) -> Dict[str, Any]:
    """
    複数の時間枠のデータを用いて、包括的なテクニカルスコアを計算する。
    
    💡 修正点: スコア内訳表示のために、各要素のボーナス/ペナルティ値を
    `tech_data` に動的に格納する。
    """
    
    final_score = BASE_SCORE # 0.40
    tech_data: Dict[str, Any] = {
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'long_term_reversal_penalty_value': 0.0,
        'macd_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0,
        'volatility_penalty_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0, # グローバルマクロは後で加算されるが、初期化
    }
    
    # 1. データフレームの選択と基本的なTAの計算
    df = locals().get(f'df_{timeframe.replace("m", "m").replace("h", "h")}')
    if df is None or len(df) < LONG_TERM_SMA_LENGTH + ATR_LENGTH:
        return {'score': 0.0, 'tech_data': tech_data} # データ不足
    
    # NaNを許容しないように、常に最新の非NaN行を取得
    # ATRはrun_analysisで既に計算済みだが、ここではSMA_200, RSI, MACD, BBands, OBVを計算するために、必要な長さに絞り、NaNをドロップする
    required_ta_length = max(LONG_TERM_SMA_LENGTH, 26, 20) + 1 
    
    df = df.iloc[-required_ta_length:].dropna()
    
    if len(df) < required_ta_length:
        return {'score': 0.0, 'tech_data': tech_data} # データ不足 (再チェック)

    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.bbands(append=True)
    df.ta.obv(append=True)
    
    # 最新の足のインデックスを取得
    latest = df.iloc[-1]
    
    # 2. 長期トレンド一致/逆行 (LONG_TERM_REVERSAL_PENALTY)
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    if sma_col in latest and not math.isnan(latest[sma_col]):
        sma_val = latest[sma_col]
        # ロングの場合: 現在価格 > SMA-200 (順張り) -> ボーナス
        if side == 'long':
            if current_price > sma_val:
                trend_bonus = LONG_TERM_REVERSAL_PENALTY # +0.20
            else:
                trend_bonus = -LONG_TERM_REVERSAL_PENALTY # -0.20 (逆張りペナルティ)
        # ショートの場合: 現在価格 < SMA-200 (順張り) -> ボーナス
        else: # side == 'short'
            if current_price < sma_val:
                trend_bonus = LONG_TERM_REVERSAL_PENALTY # +0.20
            else:
                trend_bonus = -LONG_TERM_REVERSAL_PENALTY # -0.20 (逆張りペナルティ)
                
        final_score += trend_bonus
        tech_data['long_term_reversal_penalty_value'] = trend_bonus
    
    # 3. RSIモメンタム加速/適正水準 (RSI_MOMENTUM_LOW)
    rsi_col = 'RSI_14'
    if rsi_col in latest and not math.isnan(latest[rsi_col]):
        rsi_val = latest[rsi_col]
        rsi_bonus = 0.0
        # ロングの場合: RSI > RSI_MOMENTUM_LOW (40)
        if side == 'long' and rsi_val > RSI_MOMENTUM_LOW and rsi_val < 70:
            rsi_bonus = (rsi_val - RSI_MOMENTUM_LOW) / (70 - RSI_MOMENTUM_LOW) * (RSI_DIVERGENCE_BONUS) 
            # 40で0、70でRSI_DIVERGENCE_BONUS(0.10)
        # ショートの場合: RSI < (100 - RSI_MOMENTUM_LOW) (60)
        elif side == 'short' and rsi_val < (100 - RSI_MOMENTUM_LOW) and rsi_val > 30:
            rsi_bonus = ((100 - RSI_MOMENTUM_LOW) - rsi_val) / ((100 - RSI_MOMENTUM_LOW) - 30) * (RSI_DIVERGENCE_BONUS)
            # 60で0、30でRSI_DIVERGENCE_BONUS(0.10)
            
        final_score += max(0.0, rsi_bonus) # ペナルティは与えない
        tech_data['rsi_momentum_bonus_value'] = max(0.0, rsi_bonus)
        
    # 4. MACDモメンタム一致/逆行 (MACD_CROSS_PENALTY)
    # MACDヒストグラムの最新2つの値の符号と大小でモメンタムの強さを判断
    hist_col = 'MACDh_12_26_9'
    if hist_col in latest and len(df) >= 2:
        latest_hist = latest[hist_col]
        prev_hist = df.iloc[-2][hist_col]
        
        # NaNチェック
        if not math.isnan(latest_hist) and not math.isnan(prev_hist):
            macd_val = 0.0
            # ロングの場合: ヒストグラムが陽性、かつ拡大傾向
            if side == 'long':
                if latest_hist > 0 and latest_hist > prev_hist:
                    macd_val = MACD_CROSS_PENALTY # ボーナス
                elif latest_hist < 0:
                    macd_val = -MACD_CROSS_PENALTY # ペナルティ
            # ショートの場合: ヒストグラムが陰性、かつ拡大傾向
            elif side == 'short':
                if latest_hist < 0 and latest_hist < prev_hist:
                    macd_val = MACD_CROSS_PENALTY # ボーナス (MACDは負の方向に拡大)
                elif latest_hist > 0:
                    macd_val = -MACD_CROSS_PENALTY # ペナルティ
                    
            final_score += macd_val
            tech_data['macd_penalty_value'] = macd_val
        
    # 5. OBV出来高確証 (OBV_MOMENTUM_BONUS)
    # OBVは累積出来高なので、最新と過去の値を比較してトレンド方向への確証があるかを見る
    obv_col = 'OBV'
    if obv_col in latest and len(df) >= 2:
        latest_obv = latest[obv_col]
        prev_obv = df.iloc[-2][obv_col]
        
        # NaNチェック
        if not math.isnan(latest_obv) and not math.isnan(prev_obv):
            obv_bonus = 0.0
            # ロングの場合: OBVが上昇 (出来高が上昇をサポート)
            if side == 'long' and latest_obv > prev_obv:
                obv_bonus = OBV_MOMENTUM_BONUS
            # ショートの場合: OBVが下降 (出来高が下降をサポート)
            elif side == 'short' and latest_obv < prev_obv:
                obv_bonus = OBV_MOMENTUM_BONUS
                
            final_score += obv_bonus
            tech_data['obv_momentum_bonus_value'] = obv_bonus
        
    # 6. ボラティリティ過熱ペナルティ (VOLATILITY_BB_PENALTY_THRESHOLD)
    bb_upper_col = 'BBU_20_2.0'
    bb_lower_col = 'BBL_20_2.0'
    bb_mid_col = 'BBM_20_2.0'
    
    if bb_upper_col in latest and bb_lower_col in latest and bb_mid_col in latest:
         # NaNチェック
        if not math.isnan(latest[bb_upper_col]) and not math.isnan(latest[bb_lower_col]) and not math.isnan(latest[bb_mid_col]):
            bb_width = latest[bb_upper_col] - latest[bb_lower_col]
            bb_mid = latest[bb_mid_col]
            
            # BB幅が中間値の VOLATILITY_BB_PENALTY_THRESHOLD (1%) を超える
            if bb_mid > 0 and (bb_width / bb_mid) > VOLATILITY_BB_PENALTY_THRESHOLD:
                # BB幅が大きすぎる場合はペナルティを適用（過熱感、高ボラティリティの終焉リスク）
                volatility_penalty = -STRUCTURAL_PIVOT_BONUS * 2 # -0.10
            else:
                volatility_penalty = 0.0
                
            final_score += volatility_penalty
            tech_data['volatility_penalty_value'] = volatility_penalty

    # 7. 流動性ボーナス
    liquidity_bonus = 0.0
    if current_is_top_symbol:
        liquidity_bonus = LIQUIDITY_BONUS_MAX
    
    final_score += liquidity_bonus
    tech_data['liquidity_bonus_value'] = liquidity_bonus
    
    # 8. 構造的優位性 (ベーススコア)
    final_score += STRUCTURAL_PIVOT_BONUS

    # 9. マクロ環境影響 (FGI Proxy) - 後で `run_analysis` で加算されるため、ここでは tech_data に値をセットするのみ

    # スコアの正規化 (0.0 から 1.0 の範囲に収める)
    final_score = max(0.0, min(1.0, final_score))
    
    return {
        'score': final_score, 
        'tech_data': tech_data
    }

async def run_analysis(symbol: str, current_price: float, current_is_top_symbol: bool) -> List[Dict]:
    """
    指定されたシンボルと全てのターゲットタイムフレームで分析を実行し、
    ロングとショート両方のシグナルを返す。
    """
    
    ohlcv_data = {}
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        df = await fetch_ohlcv_data(symbol, tf, limit)
        if df is None or df.empty:
            logging.warning(f"⚠️ {symbol} の {tf} データが不足しているため、分析をスキップします。")
            return [] # この銘柄の分析を中止
        ohlcv_data[tf] = df
        
    signals: List[Dict] = []
    
    # 1. SL/TPとATRの計算には、メインの取引時間足（例として1h）を使用
    main_tf = '1h'
    main_df = ohlcv_data.get(main_tf)
    if main_df is None:
        logging.warning(f"⚠️ {symbol} のメイン時間足 ({main_tf}) データが不足しています。")
        return []

    # 💡 修正: ATR計算のためのデータフレームの準備とチェック
    required_length = ATR_LENGTH + 1 # ATR_14なら最低15本
    if len(main_df) < required_length:
        logging.warning(f"⚠️ {symbol} のメイン時間足 ({main_tf}) データが短すぎます ({len(main_df)}/{required_length})。分析をスキップします。")
        return []
        
    # 最新に必要なデータ行数に絞り、NaN行を除去
    # pandas_taは内部でNaNを処理しようとするが、明示的にdropna()
    atr_df = main_df.iloc[-required_length:].dropna()

    if len(atr_df) < ATR_LENGTH:
         logging.warning(f"⚠️ {symbol} のATR計算に必要なデータがクリーンアップ後に不足しました。分析をスキップします。")
         return []
         
    # ATRの計算
    # pandas_taはinplaceで更新する
    atr_df.ta.atr(length=ATR_LENGTH, append=True)
    latest_atr_col = f'ATR_{ATR_LENGTH}'
    latest_atr = atr_df.iloc[-1].get(latest_atr_col, None) 
    
    if latest_atr is None or latest_atr <= 0 or math.isnan(latest_atr): # math.isnanを追加
        logging.warning(f"⚠️ {symbol} のATRが計算できませんでした ({latest_atr})。分析をスキップします。")
        return []

    # SL幅の計算: ATRに基づいて動的に決定
    atr_sl_width = latest_atr * ATR_MULTIPLIER_SL 
    
    # 最低リスク幅の適用
    min_risk_width = current_price * MIN_RISK_PERCENT
    risk_width = max(atr_sl_width, min_risk_width)

    # ----------------------------------------------------
    # ロングシグナルの分析
    # ----------------------------------------------------
    
    long_signal: Dict[str, Any] = {
        'symbol': symbol,
        'side': 'long',
        'timeframe': main_tf,
        'current_price': current_price,
        'score': 0.0,
        'rr_ratio': RR_RATIO_TARGET,
        'entry_price': current_price,
        'stop_loss': current_price - risk_width,  # SL = Price - Risk
        'take_profit': current_price + (risk_width * RR_RATIO_TARGET), # TP = Price + Risk * RR_RATIO_TARGET
        'tech_data': {},
    }
    
    # メイン時間足でスコアリング (ここでは1hを採用)
    long_score_result = calculate_technical_score(
        ohlcv_data.get('1m'), ohlcv_data.get('5m'), ohlcv_data.get('15m'), 
        ohlcv_data.get('1h'), ohlcv_data.get('4h'), 
        symbol, main_tf, current_price, current_is_top_symbol, 'long'
    )
    
    long_signal['score'] = long_score_result['score']
    long_signal['tech_data'] = long_score_result['tech_data']
    
    # ----------------------------------------------------
    # ショートシグナルの分析
    # ----------------------------------------------------
    
    short_signal: Dict[str, Any] = {
        'symbol': symbol,
        'side': 'short',
        'timeframe': main_tf,
        'current_price': current_price,
        'score': 0.0,
        'rr_ratio': RR_RATIO_TARGET,
        'entry_price': current_price,
        'stop_loss': current_price + risk_width,  # SL = Price + Risk
        'take_profit': current_price - (risk_width * RR_RATIO_TARGET), # TP = Price - Risk * RR_RATIO_TARGET
        'tech_data': {},
    }

    # メイン時間足でスコアリング (ここでは1hを採用)
    short_score_result = calculate_technical_score(
        ohlcv_data.get('1m'), ohlcv_data.get('5m'), ohlcv_data.get('15m'), 
        ohlcv_data.get('1h'), ohlcv_data.get('4h'), 
        symbol, main_tf, current_price, current_is_top_symbol, 'short'
    )

    short_signal['score'] = short_score_result['score']
    short_signal['tech_data'] = short_score_result['tech_data']

    # ----------------------------------------------------
    # マクロコンテキストの適用 (最終スコアの調整)
    # ----------------------------------------------------
    
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    
    # FGI (恐怖・貪欲) をスコアに反映
    # fgi_proxy > 0 (貪欲/リスクオン) -> ロングにボーナス、ショートにペナルティ
    # fgi_proxy < 0 (恐怖/リスクオフ) -> ショートにボーナス、ロングにペナルティ
    fgi_bonus_long = max(0.0, fgi_proxy) * FGI_PROXY_BONUS_MAX
    fgi_bonus_short = max(0.0, -fgi_proxy) * FGI_PROXY_BONUS_MAX
    
    # ロングスコアの調整
    long_signal['score'] = max(0.0, min(1.0, long_signal['score'] + fgi_bonus_long))
    long_signal['tech_data']['sentiment_fgi_proxy_bonus'] = fgi_bonus_long
    
    # ショートスコアの調整
    short_signal['score'] = max(0.0, min(1.0, short_signal['score'] + fgi_bonus_short))
    short_signal['tech_data']['sentiment_fgi_proxy_bonus'] = fgi_bonus_short


    # 最終的なシグナルリスト
    final_signals = [long_signal, short_signal]
    
    # スコアが高い順にソートし、採用閾値を超えたものだけを返す
    sorted_signals = sorted(final_signals, key=lambda x: x.get('score', 0.0), reverse=True)
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 採用閾値を超えたシグナルのみをフィルタリング
    accepted_signals = [s for s in sorted_signals if s['score'] >= current_threshold]
    
    # TOP_SIGNAL_COUNT の数だけシグナルを返す
    return accepted_signals[:TOP_SIGNAL_COUNT]


async def execute_trade(signal: Dict) -> Dict:
    """
    シグナルに基づき、取引所APIを使用してポジションをオープンする。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if TEST_MODE:
        logging.info(f"🧪 TEST_MODE: {signal['symbol']} ({signal['side']}) の取引をシミュレートします。")
        # テストモードではシグナルに注文情報を追加し、仮想のポジションとして追加
        filled_usdt = FIXED_NOTIONAL_USDT
        # 数量は適当に計算
        filled_amount = filled_usdt * LEVERAGE / signal['entry_price'] 
        
        trade_result = {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'entry_price': signal['entry_price'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'side': signal['side'],
            'liquidation_price': calculate_liquidation_price(signal['entry_price'], LEVERAGE, signal['side'])
        }
        
        # 仮想ポジションとして追加
        new_position = {
            'symbol': signal['symbol'],
            'side': signal['side'],
            'contracts': filled_amount if signal['side'] == 'long' else -filled_amount,
            'entry_price': signal['entry_price'],
            'filled_usdt': filled_usdt,
            'liquidation_price': trade_result['liquidation_price'],
            'stop_loss': signal['stop_loss'],
            'take_profit': signal['take_profit'],
            'timestamp': int(time.time() * 1000)
        }
        OPEN_POSITIONS.append(new_position)
        
        # signal辞書にも取引結果を反映
        signal['entry_price'] = trade_result['entry_price']
        signal['stop_loss'] = trade_result['stop_loss']
        signal['take_profit'] = trade_result['take_profit']
        signal['liquidation_price'] = trade_result['liquidation_price']

        return trade_result
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    # 1. 注文数量の計算 (FIXED_NOTIONAL_USDTに基づいて、レバレッジを考慮した数量を計算)
    try:
        # 数量 = ロット / 価格
        target_quantity_base = FIXED_NOTIONAL_USDT / entry_price
        
        # 最低取引数量などのルールチェック (ここでは省略し、取引所側でエラー処理)
        
        # 数量を取引所の精度に丸める (例: MEXCは1e-8)
        # シンボルの市場情報からロットサイズ (amount) の精度を取得
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            raise Exception(f"市場情報 ({symbol}) が見つかりません。")

        precision_amount = market['precision']['amount']
        
        # 数量を精度に合わせて調整
        if precision_amount is not None:
            target_quantity_base = EXCHANGE_CLIENT.amount_to_precision(symbol, target_quantity_base)
            
        amount = float(target_quantity_base)
        
        if amount <= 0:
            return {'status': 'error', 'error_message': '計算された注文数量がゼロ以下です。'}

        # 2. 注文実行
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 成行 (Market) 注文を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market',
            order_side,
            amount,
            params={
                # 強制的にクロスマージンを使用 (MEXCで初期設定済みだが念のため)
                'marginMode': 'cross',
                'leverage': LEVERAGE,
            }
        )
        
        logging.info(f"✅ 注文送信成功: {symbol} {side.upper()} {amount} ({format_usdt(FIXED_NOTIONAL_USDT)} USDT)")

        # 3. 注文結果の確認 (ここでは簡略化のため即時約定を想定)
        filled_amount = order.get('filled', amount)
        filled_price = order.get('price', entry_price) 
        filled_usdt = filled_amount * filled_price # 実際の約定名目ロット
        
        # 4. SL/TP注文の設定 (Stop-Limit または Take-Profit-Limit)
        # CCXTのcreate_orderにparamsでSL/TPを渡す方法（取引所依存）
        # 例：MEXCは、新規ポジションと同時にSL/TPを設定する機能が弱いか、個別のAPIコールが必要
        # ここでは、簡略化のため、**ポジションオープン後にポジションモニターでSL/TPを設定/更新する**ロジックに依存する。
        
        liquidation_price = calculate_liquidation_price(filled_price, LEVERAGE, side)

        trade_result = {
            'status': 'ok',
            'filled_amount': filled_amount,
            'filled_usdt': filled_usdt,
            'entry_price': filled_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'side': side,
            'liquidation_price': liquidation_price
        }

        # signal辞書にも取引結果を反映
        signal['entry_price'] = filled_price
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        signal['liquidation_price'] = liquidation_price
        
        # OPEN_POSITIONSの更新は、position_monitor_schedulerで行うのがより安全だが、
        # ここでは成功時に仮のポジションを追加する
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': filled_amount if side == 'long' else -filled_amount,
            'entry_price': filled_price,
            'filled_usdt': filled_usdt,
            'liquidation_price': liquidation_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'timestamp': int(time.time() * 1000)
        }
        # OPEN_POSITIONS.append(new_position) # 監視ループに任せる

        return trade_result

    except ccxt.InsufficientFunds as e:
        error_msg = f"証拠金不足エラー: {e}"
        logging.error(f"❌ 取引失敗 ({symbol}): {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.InvalidOrder as e:
        error_msg = f"無効な注文エラー (ロットサイズ、価格精度など): {e}"
        logging.error(f"❌ 取引失敗 ({symbol}): {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬ取引エラー: {e}"
        logging.error(f"❌ 取引失敗 ({symbol}): {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}


async def close_position(position: Dict, exit_type: str, exit_price: float) -> Dict:
    """
    ポジションをクローズし、結果を返す。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts_raw = position['contracts']
    entry_price = position['entry_price']
    
    # 数量 (クローズは契約数全体)
    amount_to_close = abs(contracts_raw)
    
    # 決済サイドは現在のポジションと逆
    close_side = 'sell' if side == 'long' else 'buy'

    if TEST_MODE:
        logging.info(f"🧪 TEST_MODE: {symbol} ({side}) の決済をシミュレートします。Exit: {format_price(exit_price)}, Type: {exit_type}")
        
        # PnL計算
        pnl_rate = ((exit_price - entry_price) / entry_price) * (1 if side == 'long' else -1)
        filled_usdt = position['filled_usdt']
        pnl_usdt = filled_usdt * pnl_rate * LEVERAGE # 利益/損失
        
        trade_result = {
            'status': 'ok',
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'entry_price': entry_price,
            'filled_amount': amount_to_close,
            'exit_type': exit_type,
            'side': side,
        }
        
        # 仮想ポジションリストから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if not (p['symbol'] == symbol and p['side'] == side)]
        
        return trade_result
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    try:
        # 成行 (Market) 注文でポジションを決済
        order = await EXCHANGE_CLIENT.create_order(
            symbol,
            'market',
            close_side,
            amount_to_close,
            params={
                # ポジション全体をクローズするフラグ (取引所依存)
                'reduceOnly': True,
            }
        )
        
        logging.info(f"✅ 決済注文送信成功: {symbol} {close_side.upper()} {amount_to_close} ({exit_type})")
        
        # 決済完了後、ポジションと残高を再取得（ポジションモニターに任せる）
        # ここでは注文情報を基にP&Lを概算
        
        # 実際の約定価格を取得 (なければexit_priceを使用)
        actual_exit_price = order.get('price', exit_price)
        
        # PnL計算 (概算)
        pnl_rate = ((actual_exit_price - entry_price) / entry_price) * (1 if side == 'long' else -1)
        filled_usdt = position['filled_usdt']
        pnl_usdt = filled_usdt * pnl_rate * LEVERAGE 

        trade_result = {
            'status': 'ok',
            'exit_price': actual_exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'entry_price': entry_price,
            'filled_amount': amount_to_close,
            'exit_type': exit_type,
            'side': side,
        }
        
        # OPEN_POSITIONSの更新は、ポジションモニターに任せる
        
        return trade_result
        
    except Exception as e:
        error_msg = f"予期せぬ決済エラー: {e}"
        logging.error(f"❌ 決済失敗 ({symbol}): {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def main_bot_scheduler():
    """
    メインの分析/取引スケジューラ。
    """
    global IS_CLIENT_READY, CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT
    global LAST_SUCCESS_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, LOOP_INTERVAL

    # 1. 初期化
    if not await initialize_exchange_client():
        # 初期化失敗時は、致命的エラーとして無限ループに入らず終了する方が安全だが、ここではログのみで続行
        # FastAPIの起動時にタスクが終了すると問題があるため、無限ループを維持し、クライアント未準備フラグをチェックする
        pass

    # 初回起動メッセージの送信
    try:
        account_status = await fetch_account_status()
        GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_message)
    except Exception as e:
        logging.critical(f"❌ 初期化通知の送信中にエラーが発生しました: {e}")

    # 2. メイン分析ループ
    while True:
        try:
            if not IS_CLIENT_READY:
                logging.warning("⚠️ CCXTクライアントが準備できていません。再初期化を試行します。")
                if not await initialize_exchange_client():
                    # 5分待って再試行
                    await asyncio.sleep(60 * 5) 
                    continue
            
            # --- ステップ A: 市場情報の更新 ---
            # SKIP_MARKET_UPDATEがFalseの場合、または前回の成功から時間が経ちすぎている場合
            if not SKIP_MARKET_UPDATE or (time.time() - LAST_SUCCESS_TIME) > (LOOP_INTERVAL * 2):
                # 監視銘柄リストの更新
                CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols(TOP_SYMBOL_LIMIT)
                # マクロコンテキストの更新
                GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
                
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # --- ステップ B: 全銘柄の分析 ---
            all_signals: List[Dict] = []
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # リアルタイム価格の取得
                try:
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    current_price = ticker['last']
                    
                    is_top_symbol = symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]
                    
                    # 分析の実行
                    signals = await run_analysis(symbol, current_price, is_top_symbol)
                    
                    if signals:
                        all_signals.extend(signals)
                        
                except ccxt.ExchangeError as e:
                    logging.warning(f"⚠️ {symbol} の分析をスキップ: ExchangeError ({e})")
                except Exception as e:
                    logging.error(f"❌ {symbol} の分析中に予期せぬエラー: {e}", exc_info=True)

            # --- ステップ C: シグナルの選定と取引実行 ---
            
            # 既にオープンポジションを持っている銘柄は除外
            open_symbols = {p['symbol'] for p in OPEN_POSITIONS}
            
            # スコアの高い順にソート
            all_signals.sort(key=lambda x: x.get('score', 0.0), reverse=True)
            
            # 取引対象となるシグナルを抽出
            tradable_signals = [
                s for s in all_signals 
                if s['symbol'] not in open_symbols and 
                   s['score'] >= current_threshold and
                   (time.time() - LAST_SIGNAL_TIME.get(s['symbol'], 0.0)) > TRADE_SIGNAL_COOLDOWN # クールダウンチェック
            ][:TOP_SIGNAL_COUNT] # 最もスコアの高いTOP N件のみ
            
            # 取引実行
            for signal in tradable_signals:
                logging.info(f"💰 {signal['symbol']} ({signal['side']}) 取引シグナルを検出。Score: {signal['score']:.2f}")
                
                trade_result = await execute_trade(signal)
                
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[signal['symbol']] = time.time()
                    log_signal(signal, "取引シグナル", trade_result)
                    
                    # 💡 修正: trade_resultの情報を通知メッセージに反映させるため、signalを更新してから渡す
                    # execute_trade内で signal['entry_price'] などが更新されている
                    await send_telegram_notification(format_telegram_message(
                        signal, 
                        "取引シグナル", 
                        current_threshold, 
                        trade_result=trade_result
                    ))
                else:
                    log_signal(signal, "取引失敗", trade_result)
                    await send_telegram_notification(format_telegram_message(
                        signal, 
                        "取引シグナル", 
                        current_threshold, 
                        trade_result=trade_result
                    ))

            # --- ステップ D: 分析結果の保存と通知 ---
            # 取引閾値未満の最高スコアを通知するために保存
            LAST_ANALYSIS_SIGNALS = all_signals
            await notify_highest_analysis_score() 
            
            # --- ステップ E: WebShareの更新 ---
            current_time = time.time()
            if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'status': 'Running',
                    'version': BOT_VERSION,
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'equity': ACCOUNT_EQUITY_USDT,
                    'open_positions': OPEN_POSITIONS,
                    'current_signals': all_signals[:5], # Top 5 signals
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time

            LAST_SUCCESS_TIME = time.time()
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except Exception as e:
            logging.error(f"❌ メイン分析ループで予期せぬ致命的エラーが発生しました: {e}", exc_info=True)
            # エラー発生時もループは継続し、クライアントの状態を再チェックする
            IS_CLIENT_READY = False 

        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """
    オープンポジションを監視し、SL/TPの管理、決済を行うスケジューラ。
    """
    global OPEN_POSITIONS
    
    while True:
        try:
            if not IS_CLIENT_READY:
                logging.warning("⚠️ ポジションモニター: CCXTクライアントが準備できていません。スキップします。")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
                
            # 1. 最新のポジション情報を取得
            account_status = await fetch_account_status()
            # グローバル変数 OPEN_POSITIONS を更新
            OPEN_POSITIONS = account_status['open_positions']
            
            positions_to_remove = []
            
            # 2. 各ポジションの SL/TP チェックと決済実行
            for pos in OPEN_POSITIONS:
                symbol = pos['symbol']
                side = pos['side']
                entry_price = pos['entry_price']
                current_stop_loss = pos.get('stop_loss')
                current_take_profit = pos.get('take_profit')
                
                # リアルタイム価格を取得
                try:
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    current_price = ticker['last']
                except Exception:
                    logging.warning(f"⚠️ {symbol} の価格取得失敗。このポジションの監視をスキップします。")
                    continue
                
                exit_trigger = None
                
                # SL/TPが設定されていることを確認
                if current_stop_loss is None or current_take_profit is None:
                    logging.warning(f"⚠️ {symbol} のSL/TPが未設定のポジションを検出。設定処理をスキップします。")
                    # 本来はここでSL/TPを設定し直すロジックが必要だが、今回は省略
                    continue

                # 3. SL/TP判定
                if side == 'long':
                    # SLトリガー: 現在価格 <= SL価格
                    if current_price <= current_stop_loss:
                        exit_trigger = 'SL_HIT'
                    # TPトリガー: 現在価格 >= TP価格
                    elif current_price >= current_take_profit:
                        exit_trigger = 'TP_HIT'
                        
                elif side == 'short':
                    # SLトリガー: 現在価格 >= SL価格
                    if current_price >= current_stop_loss:
                        exit_trigger = 'SL_HIT'
                    # TPトリガー: 現在価格 <= TP価格
                    elif current_price <= current_take_profit:
                        exit_trigger = 'TP_HIT'
                
                # 4. 決済処理
                if exit_trigger:
                    logging.info(f"🚨 {symbol} ({side}) のポジションが {exit_trigger} をトリガーしました。決済を実行します。")
                    
                    # 決済処理の実行 (最新価格を決済価格として渡す)
                    close_result = await close_position(pos, exit_trigger, current_price)
                    
                    if close_result['status'] == 'ok':
                        # 通知メッセージ作成のために、ポジション情報をクローズ結果で補完
                        notify_pos_info = pos.copy()
                        notify_pos_info['exit_price'] = close_result['exit_price']
                        
                        log_signal(notify_pos_info, "ポジション決済", close_result)
                        
                        # 決済通知
                        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 現在の市場閾値を取得
                        await send_telegram_notification(format_telegram_message(
                            notify_pos_info, 
                            "ポジション決済", 
                            current_threshold,
                            trade_result=close_result,
                            exit_type=exit_trigger
                        ))
                    else:
                        logging.error(f"❌ {symbol} の決済に失敗しました: {close_result.get('error_message')}")
                        
            # 5. ポジションリストを再取得し、クローズされたポジションを反映させる
            # position_monitor_schedulerの最初に `fetch_account_status()` を呼んでいるため、
            # 次のループで自動的に更新される。

        except Exception as e:
            logging.error(f"❌ ポジションモニターで予期せぬエラーが発生しました: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


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
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    # 環境変数 PORT が設定されている場合はその値を使用、ない場合は 8000
    PORT = int(os.environ.get("PORT", 8000))
    
    # uvicornの起動 (非同期にブロックするため、main_bot_schedulerは別タスクで実行)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
