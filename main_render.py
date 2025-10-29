# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. ✅ 修正済み: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. ✅ 修正済み: KeyError: 'BBB_20_2.0' (BBands計算追加とカラム名フォールバック)
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
BOT_VERSION = "v20.0.43"            # 💡 BOTバージョンを更新 
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
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0, # ボット内で動的に設定/管理
                    'take_profit': 0.0, # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE),
                    'raw_position': p,
                })
        
        # グローバル変数に保存 (ポジション管理に使用)
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions

        logging.info(f"✅ オープンポジション ({len(OPEN_POSITIONS)}件) の情報を取得しました。")
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': OPEN_POSITIONS,
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中に予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': [], 'error': True}


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None

    try:
        # MEXCではシンボル末尾に':USDT'が必要な場合がある
        ccxt_symbol = symbol 
        
        # BTC/USDT -> BTC/USDT:USDT (MEXCのFuture/Swapシンボル形式)
        if EXCHANGE_CLIENT.id == 'mexc':
            # fetch_ohlcvが標準形式 (BTC/USDT) を受け入れるか確認
            # 多くのCCXT実装では、fetch_ohlcvは標準シンボルを使用できる
            # 例外的にBinanceのSwapでは 'BTC/USDT' が 'BTCUSDT' となる場合があるが、
            # CCXTが内部的にマッピングするため、標準形式で試行する
            pass
            
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ OHLCVデータ取得失敗: {symbol} ({timeframe}, {limit}本) - データが空です。")
            return None
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        logging.info(f"✅ OHLCVデータ取得完了 ({symbol}/{timeframe} - {len(df)}本)")
        return df
        
    except ccxt.ExchangeError as e:
        # 例: 無効なシンボル
        logging.error(f"❌ OHLCVデータ取得中の取引所エラー: {symbol} - {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCVデータ取得中のネットワークエラー: {symbol} - {e}")
    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得中に予期せぬエラー: {symbol} - {e}", exc_info=True)
        
    return None

async def fetch_available_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所の出来高TOP N銘柄を取得し、監視シンボルリストを更新する。
    """
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ シンボル取得失敗: CCXTクライアントが準備できていません。既存リストを使用。")
        return CURRENT_MONITOR_SYMBOLS
    
    logging.info("ℹ️ 出来高TOP銘柄リストを更新中...")
    
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers()

        # 【修正1】tickers が None の場合に処理を中断し、エラーを避ける
        if tickers is None: 
            logging.error("❌ 監視シンボルリスト取得中にエラー: fetch_tickers() が None を返しました。既存のシンボルリストを使用します。")
            return CURRENT_MONITOR_SYMBOLS

        # 出来高 (quoteVolume) の降順でソート
        # USDTペアかつ出来高がNoneでないものをフィルタリング
        sorted_tickers = sorted(
            [t for t in tickers.values() if t and t.get('symbol') and t.get('quoteVolume') and t['symbol'].endswith('/USDT')],
            key=lambda x: x['quoteVolume'], 
            reverse=True
        )
        
        # TOP N シンボルを抽出
        top_symbols = [t['symbol'] for t in sorted_tickers][:limit]
        
        # 既存のDEFAULT_SYMBOLSとマージ (DEFAULT_SYMBOLSを優先的に含める)
        # set() を使って重複を排除し、リストに戻す
        new_monitor_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        # 監視シンボルリストを更新
        CURRENT_MONITOR_SYMBOLS = new_monitor_symbols
        logging.info(f"✅ 監視シンボルリストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 件。")
        
        return CURRENT_MONITOR_SYMBOLS
        
    except Exception as e:
        # 元のログのエラーメッセージを含める
        logging.error(f"❌ 監視シンボルリスト取得中に予期せぬエラー: '{e}'。既存リストを使用します。", exc_info=True)
        return CURRENT_MONITOR_SYMBOLS
        
async def fetch_fgi_context() -> Dict:
    """
    Fear & Greed Index (FGI) を取得し、マクロコンテキストとして保存する。
    """
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json().get('data', [])
        
        if data:
            fgi_value = int(data[0]['value'])
            fgi_value_text = data[0]['value_classification']
            
            # FGI (0-100) を -0.5 から +0.5 の範囲に正規化 (代理変数: Proxy)
            fgi_proxy = (fgi_value - 50) / 100 
            
            # 💡 簡略化のため、為替ボーナスは現在スキップ
            forex_bonus = 0.0
            
            GLOBAL_MACRO_CONTEXT.update({
                'fgi_proxy': fgi_proxy,
                'fgi_raw_value': f"{fgi_value} ({fgi_value_text})",
                'forex_bonus': forex_bonus,
            })
            
            logging.info(f"✅ FGIマクロコンテキストを更新: Raw={fgi_value}, Proxy={fgi_proxy:+.4f}")
            return GLOBAL_MACRO_CONTEXT
            
        else:
            logging.warning("⚠️ FGIデータ取得失敗: APIデータが空です。")
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得中のリクエストエラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中に予期せぬエラー: {e}", exc_info=True)
        
    return GLOBAL_MACRO_CONTEXT

# ====================================================================================
# TECHNICAL ANALYSIS
# ====================================================================================

def calculate_long_term_trend(df: pd.DataFrame, current_price: float, side: str) -> Tuple[float, Optional[float]]:
    """
    長期移動平均線(SMA 200)との乖離度と傾きを基にトレンドスコアを計算する。
    """
    
    # 1. SMA 200の計算
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    if sma_col not in df.columns:
        return 0.0, None # SMAが計算されていない場合はスキップ
        
    last = df.iloc[-1]
    last_sma = last[sma_col]
    
    if pd.isna(last_sma) or last_sma == 0:
        return 0.0, None
        
    # 2. SMAに対する価格の位置 (乖離度)
    # 価格がSMAの上にいる: 乖離度 > 0 (ロングに有利)
    # 価格がSMAの下にいる: 乖離度 < 0 (ショートに有利)
    price_to_sma_ratio = (current_price - last_sma) / last_sma
    
    # 3. 傾き (過去N期間のSMAの線形回帰)
    try:
        # 過去20本のSMA 200のデータを使用
        recent_sma = df[sma_col].dropna().iloc[-20:]
        if len(recent_sma) < 2:
            raise ValueError("Not enough SMA data for polyfit")
            
        x = np.arange(len(recent_sma))
        y = recent_sma.values
        
        # 💡 修正: np.polyfitは係数と切片を返す。戻り値のアンパックを修正。
        # 傾き (m) のみを取得すればよい
        m, c = np.polyfit(x, y, 1) 
        sma_slope = m / last_sma # 相対的な傾き
    except Exception as e:
        # logging.debug(f"SMA傾き計算エラー: {e}") 
        sma_slope = 0.0
        
    
    # 4. トレンド一致度の評価
    trend_penalty = 0.0
    
    # 順張りボーナス/逆張りペナルティの最大値
    max_penalty = LONG_TERM_REVERSAL_PENALTY 
    
    # 価格がSMAの上にあり、SMAの傾きが上向き (Longトレンド)
    is_long_trend = price_to_sma_ratio > 0 and sma_slope > 0
    # 価格がSMAの下にあり、SMAの傾きが下向き (Shortトレンド)
    is_short_trend = price_to_sma_ratio < 0 and sma_slope < 0
    
    if side == 'long':
        if is_long_trend:
            # 順張り: 傾きに応じてボーナス
            trend_penalty = min(max_penalty, max_penalty * (abs(price_to_sma_ratio * 10) + abs(sma_slope * 100)))
        elif is_short_trend:
            # 逆張り: ペナルティ
            trend_penalty = -max_penalty
            
    elif side == 'short':
        if is_short_trend:
            # 順張り: 傾きに応じてボーナス
            trend_penalty = min(max_penalty, max_penalty * (abs(price_to_sma_ratio * 10) + abs(sma_slope * 100)))
        elif is_long_trend:
            # 逆張り: ペナルティ
            trend_penalty = -max_penalty

    return trend_penalty, sma_slope

def calculate_macd_momentum(df: pd.DataFrame, side: str) -> float:
    """
    MACDラインとシグナルラインのクロス方向を基にモメンタムスコアを計算する。
    """
    
    macd_cols = ['MACD_12_26_9', 'MACDh_12_26_9', 'MACDs_12_26_9']
    if not all(col in df.columns for col in macd_cols):
        return 0.0
        
    # 最新の2つのバーのデータ
    last_two = df.iloc[[-2, -1]]
    if len(last_two) < 2:
        return 0.0

    current_macd = last_two.iloc[-1]['MACD_12_26_9']
    prev_macd = last_two.iloc[-2]['MACD_12_26_9']
    
    current_signal = last_two.iloc[-1]['MACDs_12_26_9']
    prev_signal = last_two.iloc[-2]['MACDs_12_26_9']
    
    # MACDラインとシグナルラインのクロス判定
    is_bullish_cross = prev_macd <= prev_signal and current_macd > current_signal
    is_bearish_cross = prev_macd >= prev_signal and current_macd < current_signal
    
    # MACDヒストグラムの増加/減少
    current_hist = last_two.iloc[-1]['MACDh_12_26_9']
    is_hist_increasing = current_hist > last_two.iloc[-2]['MACDh_12_26_9']
    
    momentum_bonus = 0.0
    
    if side == 'long':
        # ロングシグナルに対するMACDの一致
        if is_bullish_cross or (current_macd > current_signal and is_hist_increasing):
            momentum_bonus = MACD_CROSS_PENALTY # ボーナス
        elif is_bearish_cross or (current_macd < current_signal and not is_hist_increasing):
            momentum_bonus = -MACD_CROSS_PENALTY # ペナルティ
            
    elif side == 'short':
        # ショートシグナルに対するMACDの一致
        if is_bearish_cross or (current_macd < current_signal and not is_hist_increasing):
            momentum_bonus = MACD_CROSS_PENALTY # ボーナス
        elif is_bullish_cross or (current_macd > current_signal and is_hist_increasing):
            momentum_bonus = -MACD_CROSS_PENALTY # ペナルティ
            
    return momentum_bonus

def calculate_rsi_momentum(df: pd.DataFrame, side: str) -> float:
    """
    RSIがモメンタム加速/適正水準 (40/60) にあるかを評価する。
    """
    rsi_col = f'RSI_14'
    if rsi_col not in df.columns:
        return 0.0
        
    last_rsi = df.iloc[-1][rsi_col]
    momentum_bonus = 0.0
    
    if side == 'long':
        # ロングの場合: RSIが売られすぎを脱却し、モメンタム加速ゾーン(40-80)にある
        if last_rsi >= RSI_MOMENTUM_LOW and last_rsi <= 80:
            # 40に近いほど、反転の勢いが期待できるためボーナスを与える (単純なモメンタム判定)
            momentum_bonus = OBV_MOMENTUM_BONUS # 一律ボーナス
            
    elif side == 'short':
        # ショートの場合: RSIが買われすぎを脱却し、モメンタム加速ゾーン(20-60)にある
        if last_rsi <= (100 - RSI_MOMENTUM_LOW) and last_rsi >= 20:
            # 60に近いほど、反転の勢いが期待できるためボーナスを与える (単純なモメンタム判定)
            momentum_bonus = OBV_MOMENTUM_BONUS # 一律ボーナス
            
    return momentum_bonus

def calculate_obv_confirmation(df: pd.DataFrame, side: str) -> float:
    """
    OBV (On-Balance Volume) のトレンドが価格の方向性を確証しているかを評価する。
    """
    obv_col = f'OBV'
    if obv_col not in df.columns:
        return 0.0
        
    try:
        # 過去20本のOBVの傾きを線形回帰で確認
        recent_obv = df[obv_col].dropna().iloc[-20:]
        if len(recent_obv) < 2:
            return 0.0
            
        x = np.arange(len(recent_obv))
        y = recent_obv.values
        
        # 傾き (m) のみを取得
        m, c = np.polyfit(x, y, 1) 
        obv_slope = m 
        
    except Exception as e:
        # logging.debug(f"OBV傾き計算エラー: {e}") 
        return 0.0
        
    confirmation_bonus = 0.0
    
    # 傾きの閾値 (OBVの値の相対的な変化に基づく)
    obv_change_threshold = 0.01 
    
    if side == 'long':
        # 価格上昇 (long) に対してOBVも上昇 (出来高の確証)
        if obv_slope > obv_change_threshold:
            confirmation_bonus = OBV_MOMENTUM_BONUS
            
    elif side == 'short':
        # 価格下落 (short) に対してOBVも下落 (出来高の確証)
        if obv_slope < -obv_change_threshold:
            confirmation_bonus = OBV_MOMENTUM_BONUS
            
    return confirmation_bonus

def analyze_ohlcv(symbol: str, timeframe: str, ohlcv_data: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
    """
    OHLCVデータにテクニカル指標 (TA) を追加する。
    """
    if ohlcv_data is None or ohlcv_data.empty:
        return None
        
    df = ohlcv_data.copy()
    
    try:
        # pandas_ta を使用して主要な指標を追加
        # 出来高ベースのTA
        df.ta.adx(append=True) 
        df.ta.macd(append=True) 
        df.ta.rsi(append=True)
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        df.ta.obv(append=True)
        
        # 【修正2-c】Bollinger Bandsの計算を追加
        df.ta.bbands(length=20, std=2.0, append=True) 
        
        # ATRの計算 (リスク管理に必須)
        df.ta.atr(length=ATR_LENGTH, append=True) 
        
        # 必要な行数を確保するためにNaNを削除しない (スコアリングで最終行のみ使用)
        # df.dropna(inplace=True) # ここでは削除しない
        
        if df.empty:
            return None
            
        return df
        
    except Exception as e:
        logging.error(f"❌ {symbol}/{timeframe} のTA計算中にエラーが発生しました: {e}", exc_info=True)
        return None

def calculate_signal_score(symbol: str, df_ta: pd.DataFrame, side: str, global_macro_context: Dict) -> Tuple[float, Dict]:
    """
    テクニカル指標とマクロコンテキストに基づき、総合的なシグナルスコアを計算する。
    """
    
    if df_ta.empty or len(df_ta) < LONG_TERM_SMA_LENGTH:
        return 0.0, {'error': 'Insufficient data for long-term TA'}
        
    # 最新のバーのデータを取得
    last = df_ta.iloc[-1]
    current_price = last['Close']
    
    # NaNが含まれる場合はスコアリングできない
    if last.isnull().any():
        # NaNの数に応じて、スコアを調整するなど工夫が必要だが、ここではシンプルにスキップ
        logging.warning(f"⚠️ {symbol} - 最新バーにNaNが含まれているためスコアリングをスキップします。")
        return 0.0, {'error': 'Latest bar contains NaN values'}
        
    score = BASE_SCORE # ベーススコア (構造的優位性)
    tech_data = {'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS}
    
    # ------------------------------------------
    # 1. 長期トレンドとの一致度 (SMA 200)
    # ------------------------------------------
    long_term_penalty, sma_slope = calculate_long_term_trend(df_ta, current_price, side)
    score += long_term_penalty
    tech_data['long_term_reversal_penalty_value'] = long_term_penalty
    
    # ------------------------------------------
    # 2. MACDモメンタムとの一致
    # ------------------------------------------
    macd_penalty = calculate_macd_momentum(df_ta, side)
    score += macd_penalty
    tech_data['macd_penalty_value'] = macd_penalty
    
    # ------------------------------------------
    # 3. RSIモメンタム加速/適正水準
    # ------------------------------------------
    rsi_bonus = calculate_rsi_momentum(df_ta, side)
    score += rsi_bonus
    tech_data['rsi_momentum_bonus_value'] = rsi_bonus
    
    # ------------------------------------------
    # 4. OBV出来高確証
    # ------------------------------------------
    obv_bonus = calculate_obv_confirmation(df_ta, side)
    score += obv_bonus
    tech_data['obv_momentum_bonus_value'] = obv_bonus

    # ------------------------------------------
    # 5. ボラティリティペナルティ (BB Width)
    # ------------------------------------------
    
    # 【修正2-a/b】カラム名が存在するかを確認し、代替名 (BBW) も試す
    bb_width_col_candidates = ['BBB_20_2.0', 'BBW_20_2.0'] # BBB (旧) / BBW (新)
    bb_width_ratio = 0.0
    bb_width_col = None

    for col in bb_width_col_candidates:
        if col in last.index:
            bb_width_col = col
            # pandas_taのBBW/BBBは通常パーセント表記 (例: 1.5 -> 1.5%) のため、100で割って実数表記に戻す
            bb_width_ratio = last[col] / 100 
            break

    volatility_penalty_value = 0.0
    
    if bb_width_col is None:
        logging.warning(f"⚠️ {symbol} - BB Width カラム ('BBB_20_2.0'/'BBW_20_2.0') が見つかりません。ボラティリティペナルティをスキップします。")
    elif bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
        # ボラティリティが過熱していると判断し、ペナルティを適用
        # 例: 閾値0.01 (1%) を超えるごとにペナルティを深くする
        penalty_factor = (bb_width_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) / 0.01 
        volatility_penalty_value = -0.05 * penalty_factor 
        # ペナルティの上限を設定
        volatility_penalty_value = max(-0.15, volatility_penalty_value) 
        logging.debug(f"[{symbol}/{side}] ボラティリティペナルティ適用 ({bb_width_ratio*100:.2f}%): {volatility_penalty_value*100:+.2f}点")
    
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    score += volatility_penalty_value
    
    # ------------------------------------------
    # 6. 流動性ボーナス
    # ------------------------------------------
    
    # 流動性 (TOP SYMBOL LISTにどれだけ早く登場するか) に基づくボーナス
    liquidity_bonus_value = 0.0
    try:
        # DEFAULT_SYMBOLS内にあれば最大ボーナスを与える (流動性の高い銘柄は取引機会として優遇)
        if symbol in DEFAULT_SYMBOLS:
            liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        else:
            # CURRENT_MONITOR_SYMBOLS (出来高TOP40 + DEFAULT) の中で、TOP10内に入っていればボーナス
            symbol_rank = CURRENT_MONITOR_SYMBOLS.index(symbol) if symbol in CURRENT_MONITOR_SYMBOLS else TOP_SYMBOL_LIMIT + 1
            if symbol_rank < 10:
                liquidity_bonus_value = LIQUIDITY_BONUS_MAX * (1 - (symbol_rank / 10)) # 順位が上ほどボーナス
            
    except Exception:
        pass # エラーが発生しても続行
        
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # ------------------------------------------
    # 7. マクロコンテキスト (FGI)
    # ------------------------------------------
    
    # FGIプロキシ (例: -0.5 to +0.5) を使用
    fgi_proxy = global_macro_context.get('fgi_proxy', 0.0) + global_macro_context.get('forex_bonus', 0.0)
    sentiment_bonus = 0.0
    
    if side == 'long' and fgi_proxy > 0:
        # リスクオン (fgi_proxy > 0) はロングにボーナス
        sentiment_bonus = min(FGI_PROXY_BONUS_MAX, FGI_PROXY_BONUS_MAX * fgi_proxy * 2) 
    elif side == 'short' and fgi_proxy < 0:
        # リスクオフ (fgi_proxy < 0) はショートにボーナス
        sentiment_bonus = min(FGI_PROXY_BONUS_MAX, FGI_PROXY_BONUS_MAX * abs(fgi_proxy) * 2)

    score += sentiment_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_bonus
    
    # ------------------------------------------
    # 8. 最終スコアの調整
    # ------------------------------------------
    final_score = max(0.0, min(1.0, score))
    
    # 必須データの追加
    tech_data['last_price'] = current_price
    tech_data[f'ATR_{ATR_LENGTH}'] = last[f'ATR_{ATR_LENGTH}']
    
    return final_score, tech_data

def get_signal_timing_info(symbol: str) -> bool:
    """
    クールダウン期間中かどうかを確認する。
    """
    global LAST_SIGNAL_TIME, TRADE_SIGNAL_COOLDOWN
    
    last_time = LAST_SIGNAL_TIME.get(symbol)
    
    if last_time is None:
        return True # 初回はOK
        
    cooldown_end_time = last_time + TRADE_SIGNAL_COOLDOWN
    
    if time.time() < cooldown_end_time:
        remaining_time = timedelta(seconds=cooldown_end_time - time.time())
        logging.info(f"ℹ️ {symbol} はクールダウン期間中 ({str(remaining_time).split('.')[0]} 残り)。シグナルをスキップします。")
        return False
        
    return True

# ====================================================================================
# ENTRY & RISK MANAGEMENT
# ====================================================================================

def calculate_atr_sl_tp(entry_price: float, atr_value: float, side: str) -> Tuple[float, float, float]:
    """
    ATRに基づき、SL (Stop Loss) と TP (Take Profit) を計算する。
    
    SL = Entry Price +/- (ATR * ATR_MULTIPLIER_SL)
    TP = Entry Price +/- (ATR * ATR_MULTIPLIER_SL * RR_RATIO_TARGET)
    """
    
    # 最小リスク幅の強制 (価格のMIN_RISK_PERCENT、レバレッジ考慮前の値)
    min_risk_usd_ratio = MIN_RISK_PERCENT 
    
    # ATRベースのリスク幅
    atr_risk_abs = atr_value * ATR_MULTIPLIER_SL
    # 最小リスク幅を価格に反映
    min_risk_abs = entry_price * min_risk_usd_ratio
    
    # リスク幅は min_risk_abs と atr_risk_abs の大きい方を使用
    risk_distance = max(atr_risk_abs, min_risk_abs)

    if side == 'long':
        # Long: SLは下、TPは上
        stop_loss = entry_price - risk_distance
        take_profit = entry_price + (risk_distance * RR_RATIO_TARGET)
        
    elif side == 'short':
        # Short: SLは上、TPは下
        stop_loss = entry_price + risk_distance
        take_profit = entry_price - (risk_distance * RR_RATIO_TARGET)
        
    else:
        return 0.0, 0.0, 0.0

    # 価格が負にならないように保証
    stop_loss = max(0.0, stop_loss)
    take_profit = max(0.0, take_profit)
    
    # RR比率の計算 (実際のSL/TP幅に基づく)
    actual_risk = abs(entry_price - stop_loss)
    actual_reward = abs(take_profit - entry_price)
    rr_ratio = actual_reward / actual_risk if actual_risk > 0 else 0.0
    
    return stop_loss, take_profit, rr_ratio


async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    """
    シグナルに基づき、取引所の注文を実行する。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    
    # 最終価格とATRを取得
    entry_price = signal['tech_data']['last_price']
    atr_col = f'ATR_{ATR_LENGTH}'
    atr_value = signal['tech_data'].get(atr_col, 0.0) 
    
    if entry_price <= 0 or atr_value <= 0:
        return {'status': 'error', 'error_message': 'Entry price or ATR is invalid'}

    # 1. SL/TP/RRの決定
    sl, tp, rr = calculate_atr_sl_tp(entry_price, atr_value, side)

    if sl == 0.0 or tp == 0.0 or sl >= entry_price and side == 'long' or sl <= entry_price and side == 'short':
        # SLが不適切/計算不能な場合
        return {'status': 'error', 'error_message': 'Calculated SL/TP is invalid'}
        
    # 2. ポジションサイズ (USDT名目価値) の決定
    # 固定ロットを使用
    notional_usdt = FIXED_NOTIONAL_USDT
    
    # 3. 注文数量の計算 (CCXTは契約数を要求)
    # contracts = notional_usdt * leverage / entry_price (Simplified)
    # CCXTでは通常、基本通貨の数量 (Amount) を要求する
    # amount = notional_usdt / entry_price
    amount_base_currency = notional_usdt / entry_price
    
    if amount_base_currency <= 0:
        return {'status': 'error', 'error_message': 'Calculated amount is zero or negative.'}
        
    # 4. CCXTによる注文の実行
    order_result = {'status': 'error', 'filled_usdt': notional_usdt, 'filled_amount': amount_base_currency}

    # テストモードの場合は注文をスキップ
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side.upper()}注文をスキップしました。ロット: {format_usdt(notional_usdt)} USDT")
        order_result['status'] = 'ok'
        order_result['error_message'] = 'Test mode, no trade executed.'
        
    else:
        # 注文パラメータの決定
        order_side = 'buy' if side == 'long' else 'sell'
        order_type = 'market' 
        
        # MEXC特有の処理 (シンボルをFuture/Swap形式に)
        ccxt_symbol = symbol 
        
        # 注文の実行
        try:
            # メインの成行注文
            order = await EXCHANGE_CLIENT.create_order(
                ccxt_symbol,
                order_type,
                order_side,
                amount_base_currency,
                params={'marginMode': 'cross'} # クロスマージンモードを明示 (MEXCで設定済みだが念のため)
            )
            
            # 注文が約定したかを確認 (CCXTの戻り値は取引所により異なる)
            # 多くの場合は 'filled' か 'closed'
            if order and (order.get('status') == 'closed' or order.get('status') == 'filled'):
                order_result['status'] = 'ok'
                order_result['filled_amount'] = order.get('filled', amount_base_currency)
                order_result['filled_usdt'] = order.get('cost', notional_usdt) 
            else:
                 # 部分約定またはその他のステータスの場合はログを記録
                 logging.warning(f"⚠️ {symbol} 注文ステータス不確定: {order.get('status', 'N/A')}. フルオーダー情報: {order}")
                 order_result['status'] = 'ok' if order else 'error'
                 order_result['error_message'] = f"Order status is {order.get('status', 'N/A')}"
            
        except ccxt.ExchangeError as e:
            # Amount can not be less than zero などのエラー対策
            error_msg = str(e)
            if "Amount can not be less than zero" in error_msg:
                 error_msg = "Amount can not be less than zero: 最小注文数量を満たしていません。"
            logging.error(f"❌ {symbol} 注文失敗 - 取引所エラー: {error_msg}")
            order_result['error_message'] = error_msg
        except Exception as e:
            logging.error(f"❌ {symbol} 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
            order_result['error_message'] = str(e)


    # 5. シグナル辞書を更新して返す (SL/TP/Liq Priceを追加)
    # 注文が失敗しても、計算したSL/TPはログに残すため、シグナル辞書を更新
    signal['entry_price'] = entry_price
    signal['stop_loss'] = sl
    signal['take_profit'] = tp
    signal['rr_ratio'] = rr
    signal['liquidation_price'] = calculate_liquidation_price(entry_price, LEVERAGE, side)
    
    # 注文が成功した場合、ポジションを追跡リストに追加 (Monitor Schedulerが更新するため、ここでは仮のデータ)
    if order_result['status'] == 'ok' and not TEST_MODE:
        # Monitor Schedulerが最新のポジション情報を取得するまで待つ
        pass

    return order_result


async def monitor_and_close_positions():
    """
    オープンポジションを監視し、SLまたはTPに達した場合は決済注文を実行する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not OPEN_POSITIONS or not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return

    # 監視対象のシンボルの最新価格を一括で取得
    symbols_to_fetch = [p['symbol'] for p in OPEN_POSITIONS]
    if not symbols_to_fetch:
        return
        
    try:
        # 最新のTicker情報を取得 (価格を取得する最も簡単な方法)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.error(f"❌ ポジション監視中のTicker取得エラー: {e}")
        return

    positions_to_close: List[Dict] = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        contracts = position['contracts']
        sl = position['stop_loss']
        tp = position['take_profit']
        entry_price = position['entry_price']
        
        # Tickerから最新価格を取得
        ticker = tickers.get(symbol)
        if not ticker:
            logging.warning(f"⚠️ {symbol} の最新価格が取得できません。スキップします。")
            continue
            
        # ロングの場合は 'bid' (売値)、ショートの場合は 'ask' (買値) を使用するのが厳密だが、
        # ここではシンプルに 'last' または 'close' を使用
        current_price = ticker.get('last', ticker.get('close', 0.0))
        
        if current_price == 0.0:
            continue

        trigger_type = None

        if side == 'long':
            # Long (買い) ポジション
            # 価格がSL以下: 損切り
            if sl > 0 and current_price <= sl:
                trigger_type = 'Stop Loss (SL)'
            # 価格がTP以上: 利確
            elif tp > 0 and current_price >= tp:
                trigger_type = 'Take Profit (TP)'
                
        elif side == 'short':
            # Short (売り) ポジション
            # 価格がSL以上: 損切り
            if sl > 0 and current_price >= sl:
                trigger_type = 'Stop Loss (SL)'
            # 価格がTP以下: 利確
            elif tp > 0 and current_price <= tp:
                trigger_type = 'Take Profit (TP)'

        
        # 決済トリガーが発動した場合
        if trigger_type and not TEST_MODE:
            
            logging.info(f"🔥 {symbol} - {side.upper()} ポジション決済トリガー発動: {trigger_type} @ {format_price(current_price)}")
            
            # 決済注文の実行 (成行で反対売買)
            close_side = 'sell' if side == 'long' else 'buy'
            amount_to_close = abs(contracts)
            
            # 決済結果の辞書
            exit_result = {
                'status': 'error', 
                'exit_type': trigger_type, 
                'entry_price': entry_price,
                'exit_price': current_price,
                'filled_amount': amount_to_close,
                'pnl_usdt': 0.0,
                'pnl_rate': 0.0
            }
            
            try:
                # ポジションクローズのための成行注文
                close_order = await EXCHANGE_CLIENT.create_order(
                    symbol,
                    'market',
                    close_side,
                    amount_to_close,
                    params={'reduceOnly': True} # 決済専用フラグ
                )
                
                if close_order and (close_order.get('status') == 'closed' or close_order.get('status') == 'filled'):
                    exit_result['status'] = 'ok'
                else:
                    logging.warning(f"⚠️ {symbol} 決済注文ステータス不確定: {close_order.get('status', 'N/A')}. フルオーダー情報: {close_order}")
                    exit_result['status'] = 'error' if close_order is None else 'ok' # エラーではないと見なす
                    
                # PnLの計算 (概算)
                pnl_usdt = (current_price - entry_price) * contracts 
                pnl_rate = pnl_usdt / (position['filled_usdt'] / LEVERAGE) if position['filled_usdt'] > 0 else 0.0
                
                exit_result['pnl_usdt'] = pnl_usdt
                exit_result['pnl_rate'] = pnl_rate

            except ccxt.ExchangeError as e:
                logging.error(f"❌ {symbol} 決済注文失敗 - 取引所エラー: {e}")
                exit_result['error_message'] = str(e)
            except Exception as e:
                logging.error(f"❌ {symbol} 決済注文失敗 - 予期せぬエラー: {e}", exc_info=True)
                exit_result['error_message'] = str(e)
            
            # 決済結果をログに記録し、Telegramに通知
            log_signal(position, "ポジション決済", exit_result)
            await send_telegram_notification(
                format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), exit_result, trigger_type)
            )

            # 成功/失敗に関わらず、監視リストから削除
            positions_to_close.append(position)
            
        elif trigger_type and TEST_MODE:
            # テストモードの場合、決済処理は実行せず、ログ/通知のみ
            logging.info(f"⚠️ TEST_MODE: {symbol} - {side.upper()} 決済トリガー発動: {trigger_type} @ {format_price(current_price)} (決済スキップ)")
            
            # PnLの計算 (概算)
            pnl_usdt = (current_price - entry_price) * contracts 
            pnl_rate = pnl_usdt / (position['filled_usdt'] / LEVERAGE) if position['filled_usdt'] > 0 else 0.0
            
            exit_result_test = {
                'status': 'ok', 
                'exit_type': trigger_type, 
                'entry_price': entry_price,
                'exit_price': current_price,
                'filled_amount': abs(contracts),
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate
            }
            
            log_signal(position, "ポジション決済 (テスト)", exit_result_test)
            await send_telegram_notification(
                format_telegram_message(position, "ポジション決済 (テスト)", get_current_threshold(GLOBAL_MACRO_CONTEXT), exit_result_test, trigger_type)
            )
            positions_to_close.append(position) # テストモードでも監視リストから削除

    # 決済されたポジションをリストから削除
    if positions_to_close:
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_close]
        logging.info(f"✅ {len(positions_to_close)} 件のポジションを監視リストから削除しました。")


# ====================================================================================
# MAIN SCHEDULERS
# ====================================================================================

async def analyze_and_generate_signals(monitoring_symbols: List[str]) -> List[Dict]:
    """
    全監視銘柄のOHLCVを取得、分析し、取引シグナルを生成する。
    """
    all_signals: List[Dict] = []
    
    # 既存のオープンポジションのシンボルを取得
    open_position_symbols = {p['symbol'] for p in OPEN_POSITIONS}

    for symbol in monitoring_symbols:
        # 既にポジションを持っている銘柄はスキップ
        if symbol in open_position_symbols:
            logging.debug(f"ℹ️ {symbol} は既にポジションがあるため、新規シグナル生成をスキップします。")
            continue
            
        # クールダウン期間中の銘柄はスキップ
        if not get_signal_timing_info(symbol):
            continue

        # 各タイムフレームのOHLCVデータを並行して取得
        ohlcv_tasks = []
        for tf in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS[tf]
            ohlcv_tasks.append(fetch_ohlcv(symbol, tf, limit))
            
        ohlcv_results = await asyncio.gather(*ohlcv_tasks)
        
        # 取得したOHLCVを分析し、シグナルを生成
        for i, df_ohlcv in enumerate(ohlcv_results):
            if df_ohlcv is None or df_ohlcv.empty:
                continue
                
            timeframe = TARGET_TIMEFRAMES[i]
            
            # テクニカル分析の実行
            df_ta = analyze_ohlcv(symbol, timeframe, df_ohlcv)
            
            if df_ta is None:
                continue
                
            # 💡 シグナル判定ロジック (ここではシンプルに、LongとShortの両方を評価)
            # 実際には、特定のインジケーターのクロス等で方向を絞るべきだが、
            # ベーススコアシステムでは両方向を評価し、高い方を選ぶ
            
            # Longシグナルの評価
            score_long, tech_data_long = calculate_signal_score(symbol, df_ta, 'long', GLOBAL_MACRO_CONTEXT)
            
            # Shortシグナルの評価
            score_short, tech_data_short = calculate_signal_score(symbol, df_ta, 'short', GLOBAL_MACRO_CONTEXT)

            # よりスコアの高い方を採用
            if score_long > score_short:
                final_score = score_long
                final_side = 'long'
                final_tech_data = tech_data_long
            else:
                final_score = score_short
                final_side = 'short'
                final_tech_data = tech_data_short
            
            if final_score > 0.0:
                 # シグナル辞書を作成 (RRRはまだ計算されていない)
                signal_data = {
                    'symbol': symbol,
                    'timeframe': timeframe,
                    'side': final_side,
                    'score': final_score,
                    'tech_data': final_tech_data,
                    'timestamp': time.time(),
                }
                all_signals.append(signal_data)
                logging.debug(f"ℹ️ {symbol}/{timeframe} - {final_side.upper()} シグナルスコア: {final_score:.4f}")

    # スコアの高い順にソート
    sorted_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    
    # グローバル変数に最新の分析結果を保存
    global LAST_ANALYSIS_SIGNALS
    LAST_ANALYSIS_SIGNALS = sorted_signals
    
    return sorted_signals

async def main_bot_scheduler():
    """
    BOTのメインループを管理するスケジューラ。
    """
    global LAST_SUCCESS_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, CURRENT_MONITOR_SYMBOLS
    
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        logging.critical("❌ BOT起動失敗: CCXTクライアントの初期化に失敗しました。システムを終了します。")
        # FastAPIのプロセスを終了させる必要があるため、sys.exit()を使用する
        sys.exit(1)
        
    while True:
        try:
            current_time = time.time()
            
            # 2. 市場情報・口座ステータスの更新
            # 出来高TOP銘柄リストの更新 (1時間に1回)
            if current_time - LAST_SUCCESS_TIME > WEBSHARE_UPLOAD_INTERVAL or not IS_FIRST_MAIN_LOOP_COMPLETED:
                CURRENT_MONITOR_SYMBOLS = await fetch_available_symbols()
            
            # FGIマクロコンテキストの更新 (一定間隔)
            await fetch_fgi_context()
            
            # 口座ステータスの更新
            account_status = await fetch_account_status()
            
            # 3. BOT起動時通知 (初回のみ)
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 4. 分析とシグナル生成
            logging.info("🔬 メイン分析ループ開始...")
            
            # 銘柄リストが空でないことを確認
            if not CURRENT_MONITOR_SYMBOLS:
                logging.warning("⚠️ 監視シンボルリストが空です。分析をスキップします。")
                await asyncio.sleep(LOOP_INTERVAL)
                continue
                
            signals = await analyze_and_generate_signals(CURRENT_MONITOR_SYMBOLS)

            logging.info(f"✅ メイン分析ループ完了。合計 {len(signals)} 件のシグナルを検出。")

            # 5. 取引実行 (最高スコアのシグナルのみを評価)
            top_trade_executed = False
            for signal in signals:
                
                # 取引閾値チェック
                if signal['score'] < current_threshold:
                    logging.info(f"ℹ️ {signal['symbol']} ({signal['side']}) はスコア {signal['score']:.4f} で閾値 {current_threshold:.4f} 未満です。スキップします。")
                    continue
                
                # 最高スコア (TOP_SIGNAL_COUNT=1) の銘柄のみを取引
                if top_trade_executed:
                    continue

                # 実際に取引を実行
                logging.warning(f"🚀 {signal['symbol']} ({signal['timeframe']}/{signal['side']}) - **取引実行** (Score: {signal['score']:.4f})")
                trade_result = await process_entry_signal(signal)
                
                # ログと通知
                log_signal(signal, "取引シグナル", trade_result)
                await send_telegram_notification(
                    format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                )

                if trade_result and trade_result.get('status') == 'ok':
                    # 取引が成功した場合、クールダウン期間を開始
                    LAST_SIGNAL_TIME[signal['symbol']] = time.time()
                    top_trade_executed = True # 次のループではこの銘柄をスキップ
                    
                    
            # 6. WebShareデータの送信 (1時間に1回)
            if current_time - LAST_SUCCESS_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'open_positions': OPEN_POSITIONS,
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                    'monitoring_symbols_count': len(CURRENT_MONITOR_SYMBOLS),
                    'top_signals': LAST_ANALYSIS_SIGNALS[:5] 
                }
                await send_webshare_update(webshare_data)
                
            # 7. 定時通知の実行
            await notify_highest_analysis_score()


            # 成功時刻の更新
            LAST_SUCCESS_TIME = current_time

        except Exception as e:
            logging.error(f"❌ メイン分析ループで致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時もループを継続
            
        # 次のループまで待機
        await asyncio.sleep(LOOP_INTERVAL)

async def position_monitor_scheduler():
    """
    ポジション監視専用のスケジューラ (メインループとは独立して高速に実行)。
    """
    await asyncio.sleep(5) # メインループの起動を待つ
    
    while True:
        try:
            # オープンポジションを監視し、SL/TPトリガーをチェック
            await monitor_and_close_positions()
            
        except Exception as e:
            logging.error(f"❌ ポジション監視ループでエラーが発生しました: {e}", exc_info=True)

        # 監視間隔を短く設定
        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI & ENTRY POINT
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
    # 環境変数 PORT が設定されている場合は...
    port = int(os.getenv("PORT", 8000))
    
    # sys.argvをチェックし、FastAPI起動前にログをクリア (オプション)
    if "--no-clear-log" not in sys.argv:
        try:
            # 起動時のログファイルをクリーンアップ
            for log_file in ["apex_bot_取引シグナル_log.jsonl", "apex_bot_ポジション決済_log.jsonl"]:
                if os.path.exists(log_file):
                    os.remove(log_file)
            logging.info("✅ 既存の取引ログファイルをクリーンアップしました。")
        except Exception as e:
            logging.warning(f"⚠️ ログファイルクリーンアップ中にエラー: {e}")
            
    logging.info(f"BOTアプリケーションをポート {port} で起動します。")
    
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info", reload=False)
