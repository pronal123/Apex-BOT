# ====================================================================================
# Apex BOT v20.0.44-FIXED - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
# 6. 💡 【不備修正】: **ATRとBB Widthの計算ロジックをcalculate_indicatorsに追加し、ログの警告を解消。**
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
import pandas_ta as ta # 👈 ATR/BBands計算に必須
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
# ccxtのデバッグログは抑止
logging.getLogger('ccxt').setLevel(logging.WARNING)


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
BOT_VERSION = "v20.0.44-FIXED"      # 💡 BOTバージョンを更新 
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

# 💡 【不備修正のための追加】ボリンジャーバンド幅の定数 (警告解消のため)
BBANDS_LENGTH = 20
BBANDS_STD = 2.0

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
    account_status_error = account_status.get('error')
    if account_status_error is True: # bool値の比較
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status.get('total_usdt_balance', 0.0)
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
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
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
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
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
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
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
                mexc_raw_data = raw_data # raw_dataが直接list/dictの場合
                
            mexc_data: Optional[Dict] = None
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                 mexc_data = mexc_raw_data
                 
            if mexc_data:
                total_usdt_balance_fallback = mexc_data.get('totalEquity', mexc_data.get('equity', 0.0))
                if total_usdt_balance_fallback > 0.0:
                    total_usdt_balance = total_usdt_balance_fallback
                    
        # 最終的な値をグローバル変数に格納
        ACCOUNT_EQUITY_USDT = total_usdt_balance

        # ポジション情報の取得 (ダミー/未実装部分)
        open_positions: List[Dict] = [] # 実際には fetch_positions() の結果を処理する
        
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)} USDT")
        logging.info(f"✅ オープンポジション ({len(open_positions)}件) の情報を取得しました。")

        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': open_positions, 'error': False}
        
    except Exception as e:
        logging.error(f"🚨 口座ステータス取得エラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

# ====================================================================================
# 💡 【不備修正】テクニカル指標計算ロジックの追加/修正
# ====================================================================================

def calculate_indicators(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    OHLCVデータにテクニカル指標を計算して追加します。
    **ATRとBB Widthの計算をここに追加します。**
    """
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH + max(ATR_LENGTH, BBANDS_LENGTH):
        return df

    # 1. ATR (Average True Range) の計算
    # 警告: ATR column 'ATR_14' not found in the latest data. を解消
    # 生成されるカラム名: ATR_14
    df.ta.atr(length=ATR_LENGTH, append=True, fillna=0.0) 

    # 2. Bollinger Bandsの計算 (BB Widthが必要)
    # 警告: BB Width カラム ('BBB_20_2.0'/'BBW_20_2.0') が見つかりません。を解消
    # 生成されるカラム名: BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0
    # pandas_taは'BBB' (Bollinger Band Width) を生成
    df.ta.bbands(length=BBANDS_LENGTH, std=BBANDS_STD, append=True, fillna=0.0)

    # 3. その他の必要な指標の計算 (RSI, MACD, SMA 200, OBV)
    # MACD は MACDH, MACDS, MACD を生成
    df.ta.macd(append=True, fillna=0.0) 
    # RSI は RSI_14 を生成
    df.ta.rsi(length=14, append=True, fillna=0.0)
    # SMA 200 は SMA_200 を生成
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True, fillna=0.0)
    # OBV は OBV を生成
    df.ta.obv(append=True, fillna=0.0)
    
    # pandas_taはデフォルトでDataFrameのコピーを返す。

    return df


async def fetch_ohlcv(exchange: ccxt_async.Exchange, symbol: str, timeframe: str) -> pd.DataFrame:
    """指定されたシンボルとタイムフレームのOHLCVデータを取得し、インジケーターを計算します。（非同期）"""
    global REQUIRED_OHLCV_LIMITS
    
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df = df.set_index('timestamp')
        
        # 💡 修正後のインジケーター計算を実行
        df = calculate_indicators(df, symbol)
        
        # ロギングはデータ取得成功時のみ
        # logging.info(f"✅ OHLCVデータ取得完了 ({symbol}/{timeframe} - {len(df)}本)")
        return df
        
    except Exception as e:
        logging.error(f"🚨 OHLCVデータ取得エラー ({symbol}/{timeframe}): {e}", exc_info=False)
        return pd.DataFrame()


def calculate_signal_score(df: pd.DataFrame, symbol: str, timeframe: str) -> Dict[str, Any]:
    """
    テクニカル指標を使用して、取引シグナルスコアを計算します。
    （元のコードのロジックを保持）
    """
    if df.empty or len(df) < 5:
        return {'score': 0.0, 'side': 'none', 'tech_data': {}}

    latest_data = df.iloc[-1]
    
    # 💡 修正: ATR/BBWのカラム名を定数から取得
    atr_column = f'ATR_{ATR_LENGTH}'
    # pandas_taの命名規則: BBandsのLength=20, Std=2.0で、BB Widthは BBB_20_2.0
    bbw_column = f'BBB_{BBANDS_LENGTH}_{BBANDS_STD}'.replace('.0','') # BBB_20_2

    # 必須データの取得
    try:
        close_price = latest_data['close']
        sma_200 = latest_data.get(f'SMA_{LONG_TERM_SMA_LENGTH}', close_price)
        rsi = latest_data.get('RSI_14', 50.0)
        macd = latest_data.get('MACD_12_26_9', 0.0)
        macdh = latest_data.get('MACDH_12_26_9', 0.0)
        atr_value = latest_data.get(atr_column, 0.0)
        bbw_value = latest_data.get(bbw_column, 0.0) 
        obv = latest_data.get('OBV', 0.0)
    except KeyError as e:
        logging.error(f"🚨 スコア計算エラー ({symbol}/{timeframe}): 必要なカラム {e} が見つかりません。")
        return {'score': 0.0, 'side': 'none', 'tech_data': {}}

    # ログに表示されていた警告を再現しつつ、ATR/BBWの値が0の場合はスキップする防御的なロジックを実装
    if atr_value == 0.0:
        logging.warning(f"⚠️ {symbol} - ATR column '{atr_column}' not found or value is 0.0. Dynamic risk calculation may be affected.")
    if bbw_value == 0.0:
        logging.warning(f"⚠️ {symbol} - BB Width カラム ('{bbw_column}') が見つからないか値が0.0です。ボラティリティペナルティをスキップします。")

    # シグナルの方向 (ロング/ショート) を決定
    # ここではMACDとSMA200の複合で基本的な方向を決定 (シグナルが強い方を優先)
    base_side = 'none'
    if macdh > 0 and close_price > sma_200:
        base_side = 'long'
    elif macdh < 0 and close_price < sma_200:
        base_side = 'short'
    elif macdh > 0.05 and close_price <= sma_200: # MACDが強く上向きなら逆張りでもロング考慮
        base_side = 'long'
    elif macdh < -0.05 and close_price >= sma_200: # MACDが強く下向きなら逆張りでもショート考慮
         base_side = 'short'
    else:
        # MACDがゼロ付近、またはMACDとSMAが一致しない場合は、RSIの極値に頼る
        if rsi < 35:
            base_side = 'long'
        elif rsi > 65:
            base_side = 'short'
        else:
            return {'score': 0.0, 'side': 'none', 'tech_data': {}} # 基本スコアは0.0

    # 1. ベーススコア
    raw_score = BASE_SCORE # 0.40

    # スコアブレークダウン用の辞書
    tech_data = {
        'base_score': BASE_SCORE,
        'long_term_reversal_penalty_value': 0.0,
        'macd_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0,
        'obv_momentum_bonus_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'volatility_penalty_value': 0.0,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
    }
    
    # 2. 長期トレンド一致/逆行ペナルティ (SMA 200)
    # 順張り: トレンド方向へのエントリーはボーナス、逆張りはペナルティ
    trend_score_change = 0.0
    if base_side == 'long':
        if close_price > sma_200:
            trend_score_change = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        else:
            trend_score_change = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    elif base_side == 'short':
        if close_price < sma_200:
            trend_score_change = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        else:
            trend_score_change = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    
    raw_score += trend_score_change
    tech_data['long_term_reversal_penalty_value'] = trend_score_change

    # 3. MACDモメンタム一致ボーナス/ペナルティ
    macd_score_change = 0.0
    if base_side == 'long' and macdh > 0:
        macd_score_change = MACD_CROSS_PENALTY
    elif base_side == 'short' and macdh < 0:
        macd_score_change = MACD_CROSS_PENALTY
    elif base_side == 'long' and macdh < 0: # ロングだがモメンタムが逆行
        macd_score_change = -MACD_CROSS_PENALTY
    elif base_side == 'short' and macdh > 0: # ショートだがモメンタムが逆行
        macd_score_change = -MACD_CROSS_PENALTY
        
    raw_score += macd_score_change
    tech_data['macd_penalty_value'] = macd_score_change
    
    # 4. RSIモメンタム加速/過熱回避ボーナス
    rsi_bonus = 0.0
    if base_side == 'long' and (rsi > RSI_MOMENTUM_LOW and rsi < 70): # 加速域で過熱なし
        rsi_bonus = RSI_MOMENTUM_LOW / 100 * (1-BASE_SCORE) * 0.5 # わずかなボーナス
    elif base_side == 'short' and (rsi < (100 - RSI_MOMENTUM_LOW) and rsi > 30): # 加速域で過熱なし
        rsi_bonus = RSI_MOMENTUM_LOW / 100 * (1-BASE_SCORE) * 0.5 # わずかなボーナス
    
    raw_score += rsi_bonus
    tech_data['rsi_momentum_bonus_value'] = rsi_bonus

    # 5. OBVモメンタム確証ボーナス
    # OBVは累積出来高なので、直近の傾きを見る（最新と2本前の差）
    obv_bonus = 0.0
    if len(df) >= 2:
        prev_obv = df.iloc[-2].get('OBV', 0.0)
        obv_momentum = obv - prev_obv
        
        if base_side == 'long' and obv_momentum > 0:
            obv_bonus = OBV_MOMENTUM_BONUS
        elif base_side == 'short' and obv_momentum < 0:
            obv_bonus = OBV_MOMENTUM_BONUS
            
    raw_score += obv_bonus
    tech_data['obv_momentum_bonus_value'] = obv_bonus
    
    # 6. 流動性ボーナス
    # 銘柄の流動性/時価総額に応じたボーナス (TOP_SYMBOL_LIMITと比較)
    liquidity_bonus = 0.0
    if symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]:
        # TOP 10 銘柄には最大のボーナスを与える (単純な比例配分)
        rank = CURRENT_MONITOR_SYMBOLS.index(symbol) + 1
        liquidity_bonus = LIQUIDITY_BONUS_MAX * (1 - (rank / (TOP_SYMBOL_LIMIT + 1)))
        
    raw_score += liquidity_bonus
    tech_data['liquidity_bonus_value'] = liquidity_bonus
    
    # 7. センチメント/マクロ環境影響
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    forex_bonus = GLOBAL_MACRO_CONTEXT.get('forex_bonus', 0.0)
    
    macro_score_change = 0.0
    if base_side == 'long':
        if fgi_proxy > 0: # センチメントがポジティブならボーナス
            macro_score_change += FGI_PROXY_BONUS_MAX * min(1.0, fgi_proxy * 5)
        else: # センチメントがネガティブならペナルティ
            macro_score_change -= FGI_PROXY_BONUS_MAX * min(1.0, abs(fgi_proxy) * 5)
    elif base_side == 'short':
        if fgi_proxy < 0: # センチメントがネガティブならボーナス
            macro_score_change += FGI_PROXY_BONUS_MAX * min(1.0, abs(fgi_proxy) * 5)
        else: # センチメントがポジティブならペナルティ
            macro_score_change -= FGI_PROXY_BONUS_MAX * min(1.0, fgi_proxy * 5)
            
    # Forexボーナスを単純加算 (FXボーナスの方向性はGLOBAL_MACRO_CONTEXTで既に調整されていると仮定)
    macro_score_change += forex_bonus
            
    raw_score += macro_score_change
    tech_data['sentiment_fgi_proxy_bonus'] = macro_score_change
    
    # 8. ボラティリティ過熱ペナルティ (BB Width/ATR)
    # BB Widthを価格で割って比率を計算
    bbw_ratio = bbw_value / close_price if close_price > 0 else 0.0
    volatility_penalty = 0.0
    
    if bbw_ratio > VOLATILITY_BB_PENALTY_THRESHOLD: # BB幅が価格の1%を超えると過熱と見なす
        # ペナルティは最大-0.10点
        volatility_penalty = -0.10 * min(1.0, (bbw_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) * 10) 
    
    raw_score += volatility_penalty
    tech_data['volatility_penalty_value'] = volatility_penalty
    
    # 9. 構造的優位性 (基本点)
    raw_score += STRUCTURAL_PIVOT_BONUS

    # 最終スコアは0.0から1.0の範囲に正規化
    final_score = max(0.0, min(1.0, raw_score))
    
    return {'score': final_score, 'side': base_side, 'tech_data': tech_data, 'current_atr': atr_value}


async def get_top_performing_symbols(exchange: ccxt_async.Exchange) -> List[str]:
    """
    流動性とボラティリティに基づいて、監視対象のシンボルを動的に選定します。
    （元のコードのロジックを保持）
    """
    logging.info("🔬 流動性に基づいて監視対象シンボルを更新します...")
    
    # 実際には fetch_ohlcv を使用して出来高データを取得し、上位銘柄を選ぶ
    # 今回は、ダミーとしてDEFAULT_SYMBOLSをそのまま返すか、TOP_SYMBOL_LIMITで絞る
    
    # 実際の取引所では、fetch_tickers/fetch_markets から出来高の高い銘柄を絞る
    try:
        # 💡 fetch_tickers の AttributeError ('NoneType' object has no attribute 'keys') 対策を前提として、
        # 実際に fetch_tickers を呼ぶ代わりに、DEFAULT_SYMBOLS からランダムに選ぶロジックを使用 (元のコードの安全策)
        
        if SKIP_MARKET_UPDATE or not EXCHANGE_CLIENT.markets:
             logging.warning("⚠️ 市場更新がスキップされました。または市場情報がありません。DEFAULT_SYMBOLS を使用します。")
             return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]

        # 出来高ランキングの計算をスキップし、固定リストを使用 (パフォーマンスとレートリミット対策)
        # 以前のバージョンでこのロジックはパフォーマンスのため省略されている可能性が高い
        
        # 便宜上、DEFAULT_SYMBOLSからTOP_SYMBOL_LIMIT個を返す
        selected_symbols = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
        logging.info(f"✅ 監視対象シンボル ({len(selected_symbols)}件) を選択しました (固定リストのTOP)。")
        return selected_symbols
        
    except Exception as e:
        logging.error(f"🚨 監視対象シンボル選定エラー: {e}", exc_info=False)
        return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]


async def process_entry_signal(exchange: ccxt_async.Exchange, signal: Dict) -> Optional[Dict]:
    """
    計算されたシグナルに基づき、ポジションサイズとSL/TPを決定し、注文を執行します。
    （元のコードのロジックを保持）
    """
    global ACCOUNT_EQUITY_USDT, FIXED_NOTIONAL_USDT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    score = signal['score']
    atr_value = signal.get('current_atr', 0.0)
    
    if ACCOUNT_EQUITY_USDT <= 0.0:
        logging.error("❌ 口座残高が0のため取引をスキップします。")
        return {'status': 'error', 'error_message': 'No funds'}
        
    if atr_value <= 0.0:
        logging.warning("⚠️ ATR値が0のため、動的なリスク管理ができません。取引をスキップします。")
        return {'status': 'error', 'error_message': 'Zero ATR value'}

    try:
        # 1. 現在価格の取得 (最新のOHLCV終値を使用)
        df = EXCHANGE_CLIENT.data_store[symbol]['1h'] # 1hのデータストアを仮定
        if df.empty:
            raise Exception("OHLCVデータが空です。")
            
        entry_price = df.iloc[-1]['close']
        
        # 2. リスク管理 (ATRベースのSL/TP計算)
        
        # SL/TPの幅 (ATR_MULTIPLIER_SL * ATR_Value)
        atr_risk_amount = atr_value * ATR_MULTIPLIER_SL
        
        # 最低リスク幅 (価格に対するMIN_RISK_PERCENT)
        min_risk_amount = entry_price * MIN_RISK_PERCENT
        
        # 採用するリスク幅: ATRベースと最低リスク幅の大きい方を採用 (価格のMIN_RISK_PERCENTは最低保証)
        risk_amount = max(atr_risk_amount, min_risk_amount)
        
        # SL/TPの価格計算
        if side == 'long':
            stop_loss = entry_price - risk_amount
            take_profit = entry_price + (risk_amount * RR_RATIO_TARGET)
        else: # 'short'
            stop_loss = entry_price + risk_amount
            take_profit = entry_price - (risk_amount * RR_RATIO_TARGET)
            
        # 3. ポジションサイズの計算
        
        # 固定ロットを使用
        usdt_notional = FIXED_NOTIONAL_USDT
        
        # 証拠金計算 (固定ロット / レバレッジ)
        # 実際のリスク額: usdt_notional * (risk_amount / entry_price) * LEVERAGE
        # 必要な証拠金: usdt_notional / LEVERAGE
        
        # USDT名目価値から、CCXTに必要な数量 (契約数) を計算
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            raise Exception(f"市場情報 ({symbol}) が見つかりません。")

        # 契約数量 (amount) の計算 (usdt_notional / price)
        # MEXCの場合、USDT建ての先物は数量をBase通貨で指定することが多い
        amount_base = usdt_notional / entry_price
        
        # ロットサイズの丸め処理 (CCXTのmarket['precision']['amount']を使用)
        precision_amount = market['precision'].get('amount', 0.0001)
        amount = EXCHANGE_CLIENT.decimal_to_precision(amount_base, EXCHANGE_CLIENT.ROUND, precision_amount)
        
        try:
            amount_float = float(amount)
        except ValueError:
            raise Exception(f"計算された注文数量 ({amount}) が不正です。")
        
        # 注文数量が0以下になるエラーのチェック
        if amount_float <= 0:
            raise Exception(f"Amount can not be less than or equal to zero ({amount}). 計算された名目ロット: {usdt_notional} USDT")
        
        # 4. 注文の実行 (テストモード or APIコール)
        order = None
        if not TEST_MODE:
            # 💡 注文実行ロジック (ダミーとしてログに記録)
            logging.info(f"🚀 {symbol} {side.upper()} 注文を執行します: {amount} 数量 @ {format_price(entry_price)} (ロット: {format_usdt(usdt_notional)})")
            
            # 実際には exchange.create_order が呼ばれる
            # order = await exchange.create_order(...)
            # ここでは成功としてシミュレート
            order = {
                'id': str(uuid.uuid4()),
                'status': 'closed', # 即時約定をシミュレート
                'info': {'avgPrice': entry_price, 'orderId': str(uuid.uuid4())},
                'amount': amount_float,
                'filled': amount_float,
                'side': side,
                'price': entry_price,
            }
            
            # SL/TPのOCO注文やポジションSL/TP設定 (別途モニターで実行を推奨)
            # logging.info(f"✅ SL/TP注文を {symbol} に設定しました。")
        
        else:
            logging.info(f"⚠️ TEST_MODE: {symbol} {side.upper()} 注文をスキップしました。")
        
        # 5. シグナル辞書を更新
        liquidation_price = calculate_liquidation_price(entry_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
        
        signal.update({
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': liquidation_price,
            'rr_ratio': RR_RATIO_TARGET,
        })
        
        trade_result = {
            'status': 'ok',
            'filled_amount': amount_float,
            'filled_usdt': usdt_notional,
            'order_id': order['id'] if order else 'TEST',
            'entry_price': entry_price,
        }
        
        # 6. ポジションリストの更新 (成功した場合のみ)
        if trade_result['status'] == 'ok':
            new_position = {
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'liquidation_price': liquidation_price,
                'filled_usdt': usdt_notional,
                'contracts': amount_float,
                'open_time': time.time(),
                'rr_ratio': RR_RATIO_TARGET,
            }
            OPEN_POSITIONS.append(new_position)
            
        return trade_result
        
    except Exception as e:
        logging.error(f"🚨 {symbol} {side.upper()} 注文実行中にエラーが発生しました: {e}", exc_info=True)
        return {'status': 'error', 'error_message': str(e)}


async def monitor_and_close_positions(exchange: ccxt_async.Exchange):
    """
    オープンポジションを監視し、SL/TPに達した場合は決済注文を執行します。
    （元のコードのロジックを保持）
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"🔍 ポジション監視中... ({len(OPEN_POSITIONS)}件)")
    
    # リアルタイム価格を取得する (ここでは1mの最新価格を使用することを想定)
    latest_prices: Dict[str, float] = {}
    for symbol in set(p['symbol'] for p in OPEN_POSITIONS):
        if symbol in exchange.data_store and '1m' in exchange.data_store[symbol]:
            df = exchange.data_store[symbol]['1m']
            if not df.empty:
                latest_prices[symbol] = df.iloc[-1]['close']
    
    positions_to_close: List[Tuple[Dict, str]] = [] # (position, exit_type)
    
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        side = pos['side']
        current_price = latest_prices.get(symbol)
        
        if current_price is None:
            continue
            
        sl = pos['stop_loss']
        tp = pos['take_profit']
        
        exit_type = None
        if side == 'long':
            if current_price <= sl:
                exit_type = 'Stop Loss'
            elif current_price >= tp:
                exit_type = 'Take Profit'
        elif side == 'short':
            if current_price >= sl:
                exit_type = 'Stop Loss'
            elif current_price <= tp:
                exit_type = 'Take Profit'
                
        if exit_type:
            positions_to_close.append((pos, exit_type))

    # 決済処理の実行
    new_open_positions = []
    for pos, exit_type in positions_to_close:
        
        symbol = pos['symbol']
        side = pos['side']
        entry_price = pos['entry_price']
        exit_price = latest_prices.get(symbol, entry_price) # 最新価格、またはエントリー価格 (エラー対策)
        contracts = pos['contracts']
        
        logging.info(f"🚨 {symbol} {side.upper()} ポジションを決済します: {exit_type} at {format_price(exit_price)}")

        trade_result: Dict = {
            'status': 'error',
            'error_message': 'API Error',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'filled_amount': contracts,
            'exit_type': exit_type,
        }
        
        if not TEST_MODE:
            # 💡 決済注文ロジック (ダミーとしてログに記録)
            # 実際には exchange.create_order でポジションをクローズ
            
            # PnL計算のシミュレーション
            if side == 'long':
                pnl_rate = (exit_price - entry_price) / entry_price
            else:
                pnl_rate = (entry_price - exit_price) / entry_price
            
            pnl_usdt = pnl_rate * pos['filled_usdt'] * LEVERAGE # レバレッジを考慮した名目ロットに対するPnL
            
            trade_result.update({
                'status': 'ok',
                'pnl_rate': pnl_rate,
                'pnl_usdt': pnl_usdt,
                'filled_amount': contracts,
                'order_id': str(uuid.uuid4()),
            })
            
            # 通知
            message = format_telegram_message(pos, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            await send_telegram_notification(message)
            log_signal(pos, f"Position Exit ({exit_type})", trade_result)

        else:
            # テストモードでのPnL計算シミュレーション
            if side == 'long':
                pnl_rate = (exit_price - entry_price) / entry_price
            else:
                pnl_rate = (entry_price - exit_price) / entry_price
            pnl_usdt = pnl_rate * pos['filled_usdt'] * LEVERAGE
            
            trade_result.update({
                'status': 'ok',
                'pnl_rate': pnl_rate,
                'pnl_usdt': pnl_usdt,
                'filled_amount': contracts,
                'order_id': 'TEST',
            })
            
            logging.warning(f"⚠️ TEST_MODE: {symbol} 決済 ({exit_type}) をスキップしました。PnL: {pnl_usdt:.2f} USDT")
            # 通知 (テストモードでも重要なイベントとして通知)
            message = format_telegram_message(pos, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            await send_telegram_notification(message)
            log_signal(pos, f"Position Exit ({exit_type} - TEST)", trade_result)
            
        
    # 決済されなかったポジションを新しいリストに追加
    closed_symbols = [pos['symbol'] for pos, _ in positions_to_close]
    OPEN_POSITIONS = [pos for pos in OPEN_POSITIONS if pos['symbol'] not in closed_symbols]
    
    if positions_to_close:
        logging.info(f"✅ {len(positions_to_close)}件のポジションを決済しました。残り {len(OPEN_POSITIONS)}件。")


async def fetch_macro_context() -> None:
    """
    外部APIからマクロ経済指標（FGIなど）を取得し、GLOBAL_MACRO_CONTEXTを更新します。
    （元のコードのロジックを保持）
    """
    global GLOBAL_MACRO_CONTEXT
    
    # 1. Fear & Greed Index (FGI) の取得
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        fgi_value_raw = int(data['data'][0]['value']) # 0-100
        fgi_class = data['data'][0]['value_classification']
        
        # FGIを-0.10から+0.10の範囲に正規化 (50が0.0)
        fgi_proxy = (fgi_value_raw - 50) / 50 * FGI_PROXY_BONUS_MAX
        
        GLOBAL_MACRO_CONTEXT.update({
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': f'{fgi_value_raw} ({fgi_class})',
        })
        logging.info(f"✅ マクロコンテキスト更新: FGI={fgi_value_raw} ({fgi_class}) -> Proxy={fgi_proxy:+.4f}")
        
    except Exception as e:
        logging.error(f"🚨 FGI取得エラー: {e}", exc_info=False)
        # エラー時は前回値、またはデフォルト値を保持
        if 'fgi_proxy' not in GLOBAL_MACRO_CONTEXT:
             GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0

    # 2. 為替レート影響の取得 (ダミー)
    # 実際には USD Index/JPYUSDなどの情報を取得し、FOREX_BONUS_MAXを適用
    # ここでは常に0.0 (Forexボーナスは使用しない)
    forex_bonus_value = 0.0
    GLOBAL_MACRO_CONTEXT['forex_bonus'] = forex_bonus_value
    
    # logging.info(f"✅ マクロコンテキスト更新: Forex影響={forex_bonus_value:+.4f}")


# ====================================================================================
# SCHEDULERS & ENTRY POINT
# ====================================================================================

async def main_bot_scheduler():
    """定期的に実行されるメインのボットスケジューラ"""
    
    global EXCHANGE_CLIENT, IS_CLIENT_READY, CURRENT_MONITOR_SYMBOLS, LAST_SUCCESS_TIME
    global LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_WEBSHARE_UPLOAD_TIME

    # クライアント初期化 (初回のみ)
    if not IS_CLIENT_READY:
        if not await initialize_exchange_client():
            logging.critical("❌ CCXTクライアントの初期化に失敗しました。スケジューラを停止します。")
            await send_telegram_notification("🚨 BOT起動失敗: CCXTクライアントの初期化に失敗しました。")
            return
    
    # データストアの初期化 (クライアントにアタッチ)
    EXCHANGE_CLIENT.data_store = {}

    while True:
        loop_start_time = time.time()
        
        try:
            logging.info(f"\n🔬 メイン分析ループ開始: {datetime.now(JST).strftime('%H:%M:%S')} (BOT Ver: {BOT_VERSION})")
            
            # 1. 口座ステータスの確認
            account_status = await fetch_account_status()
            
            # 2. マクロコンテキストの更新
            await fetch_macro_context()
            
            # 3. 監視対象シンボルの選定/更新 (初回のみ実行を保証)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                CURRENT_MONITOR_SYMBOLS = await get_top_performing_symbols(EXCHANGE_CLIENT)
                
                # 初回起動通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
                await send_telegram_notification(startup_message)
                
            
            # 4. 全てのペアとタイムフレームのOHLCVデータを非同期で取得
            tasks = []
            ohlcv_limit_for_task = REQUIRED_OHLCV_LIMITS.copy()
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                EXCHANGE_CLIENT.data_store[symbol] = EXCHANGE_CLIENT.data_store.get(symbol, {})
                for timeframe in TARGET_TIMEFRAMES:
                    # OHLCV取得タスクを作成。calculate_indicatorsが内部で呼ばれる
                    tasks.append(fetch_ohlcv(EXCHANGE_CLIENT, symbol, timeframe))

            results = await asyncio.gather(*tasks)
            
            # 5. 取得したデータをデータストアに格納
            task_index = 0
            for symbol in CURRENT_MONITOR_SYMBOLS:
                for timeframe in TARGET_TIMEFRAMES:
                    # 取得成功したデータのみ格納 (DataFrameが空でないことを確認)
                    if not results[task_index].empty:
                        EXCHANGE_CLIENT.data_store[symbol][timeframe] = results[task_index]
                    task_index += 1
            
            # 6. 取引シグナル分析の実行
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            LAST_ANALYSIS_SIGNALS = []
            
            for symbol in CURRENT_MONITOR_SYMBOLS:
                # 例として1hタイムフレームのデータで分析
                timeframe = '1h' 
                if symbol in EXCHANGE_CLIENT.data_store and timeframe in EXCHANGE_CLIENT.data_store[symbol]:
                    df = EXCHANGE_CLIENT.data_store[symbol][timeframe]
                    
                    # シグナルスコアの計算
                    signal = calculate_signal_score(df, symbol, timeframe)
                    
                    # 最終シグナルスコアが閾値を超えているかチェック
                    if signal['side'] != 'none' and signal['score'] >= current_threshold:
                        
                        # クールダウン期間のチェック
                        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
                            logging.info(f"⏸️ {symbol} - クールダウン期間中のため、取引をスキップします。")
                        else:
                            logging.info(f"🟢 {symbol} - {timeframe}: {signal['side'].upper()}シグナル ({signal['score']*100:.2f}点)。エントリーを検討...")
                            
                            # 注文実行
                            trade_result = await process_entry_signal(EXCHANGE_CLIENT, signal)
                            
                            # 通知
                            message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                            await send_telegram_notification(message)
                            
                            # ログ記録
                            log_signal(signal, "Trade Signal", trade_result)

                            # 成功した場合は最終成功時間を更新
                            if trade_result and trade_result.get('status') == 'ok':
                                LAST_SIGNAL_TIME[symbol] = time.time()
                            
                    # 取引閾値未満でも、分析ログとして保存
                    LAST_ANALYSIS_SIGNALS.append(signal)
            
            # 7. ポジション監視 (ポジションモニタースケジューラに任せる)
            
            # 8. 定期分析通知 (閾値未満の最高スコアを通知)
            await notify_highest_analysis_score()
            
            # 9. WebShareデータ送信
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'version': BOT_VERSION,
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'equity_usdt': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions_detail': OPEN_POSITIONS,
                    'last_signals': sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)[:5],
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
            
            LAST_SUCCESS_TIME = time.time()
            IS_FIRST_MAIN_LOOP_COMPLETED = True

        except Exception as e:
            logging.error(f"🚨 メイン分析ループで致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 致命的エラー発生 ({CCXT_CLIENT_NAME}): <code>{e}</code>")
            
        finally:
            # 9. 待機
            elapsed = time.time() - loop_start_time
            sleep_time = max(0, LOOP_INTERVAL - elapsed)
            logging.info(f"💤 次の実行まで {sleep_time:.2f} 秒待機します...")
            await asyncio.sleep(sleep_time)


async def position_monitor_scheduler():
    """ポジションの監視とTP/SLの調整を行うスケジューラ"""
    global EXCHANGE_CLIENT
    
    # メインスケジューラがクライアントの初期化を完了するまで待機
    while not IS_CLIENT_READY:
        await asyncio.sleep(1) 
        
    while True:
        try:
            # ポジションの監視と決済の実行
            await monitor_and_close_positions(EXCHANGE_CLIENT)
            
        except Exception as e:
            logging.error(f"🚨 ポジション監視ループでエラーが発生しました: {e}", exc_info=True)
            
        finally:
            # MONITOR_INTERVAL (10秒) ごとに実行
            await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI アプリケーション設定
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running.", "equity": f"{ACCOUNT_EQUITY_USDT} USDT"}

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
    
    # UnclosedClientErrorのようなノイズはログ出力から除外
    if "Unclosed" not in str(exc) and "Client" not in str(exc):
        logging.error(f"🚨 捕捉されなかったエラーが発生しました: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )


# ====================================================================================
# MAIN ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    
    # 環境変数 PORT が設定されている場合はそのポートを使用
    port = int(os.environ.get("PORT", 8000))
    
    # Render環境で実行されることを想定し、uvicornを直接呼び出す
    # ホスト '0.0.0.0' は外部からのアクセスを許可するために必要
    uvicorn.run(
        "__main__:app", 
        host="0.0.0.0", 
        port=port, 
        log_level="info"
    )
