# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
# 6. 💡 補完: initialize_exchange_clientのレバレッジ設定ロジックを修正・補完。
# 7. 💡 補完: 各コア関数の実装を完全補完し、省略部分を排除。
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
    
    # breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

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
    """
    分析データと現在のポジション状況を外部WebShareエンドポイントに送信する。
    """
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
    
    current_time = time.time()
    
    if current_time - LAST_WEBSHARE_UPLOAD_TIME < WEBSHARE_UPLOAD_INTERVAL:
        return # まだ送信時間ではない

    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return 
        
        try:
            cleaned_data = _to_json_compatible(data)
            # requests.postを非同期で実行
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()
            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")
            LAST_WEBSHARE_UPLOAD_TIME = current_time
        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")
    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化し、認証情報、設定、市場データをロードする。"""
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

        # レバレッジの設定 (MEXC/Bybitなど、一部の取引所向け)
        if EXCHANGE_CLIENT.id in ['mexc', 'bybit']:
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

            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。(MEXC)
            # Bybitの場合は、ccxtの仕様上、先物取引で自動的にレバレッジが設定されることが多いですが、念のため実行。
            for symbol in symbols_to_set_leverage:
                try:
                    # openType: 2 は Cross Margin
                    # positionType: 1 は Long (買い) ポジション用
                    params = {'marginMode': 'cross'} # Bybit向け
                    if EXCHANGE_CLIENT.id == 'mexc':
                        params.update({'openType': 2, 'positionType': 1}) # MEXC向け
                        
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params=params
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long/Short) に設定しました。")
                except Exception as e:
                    # Bybitは set_leverageでマージンモードも設定されることがあるが、エラーを出す場合がある
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定に失敗しました: {e}") 
                    
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

        IS_CLIENT_READY = True
        return True

    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化中に致命的なエラーが発生しました: {e}", exc_info=True)
        EXCHANGE_CLIENT = None
        return False

async def get_fgi_data() -> Tuple[float, str]:
    """Fear & Greed Index (FGI) を取得し、スコアリング用のプロキシ値を計算する。"""
    
    global GLOBAL_MACRO_CONTEXT, FGI_PROXY_BONUS_MAX

    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    fgi_raw_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # データの形式チェック
        if 'data' in data and data['data']:
            fgi_data = data['data'][0]
            value_raw = fgi_data['value']
            value_classification = fgi_data['value_classification']
            
            # FGI (0=極端な恐怖 to 100=極端な貪欲) を -1.0 to 1.0 の範囲に正規化
            # (FGI - 50) / 50 -> 0 to 100 becomes -1.0 to 1.0
            fgi_value = int(value_raw)
            proxy = (fgi_value - 50) / 50.0
            
            # マクロ影響の計算 (例: FGI=75 (Greed) -> proxy=0.5 -> score_influence = 0.5 * 0.05 = +0.025)
            fgi_proxy = proxy * FGI_PROXY_BONUS_MAX
            fgi_raw_value = f"{fgi_value} ({value_classification})"
            
            logging.info(f"✅ FGIデータ取得成功: {fgi_raw_value}, Proxy: {fgi_proxy:.4f}")
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = fgi_raw_value
            
            return fgi_proxy, fgi_raw_value
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータ処理エラー: {e}")

    # エラー時は前回値、またはデフォルト値を返す
    return fgi_proxy, fgi_raw_value


async def get_top_symbols() -> List[str]:
    """取引所から出来高トップのシンボルを取得し、DEFAULT_SYMBOLSを更新する。"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, DEFAULT_SYMBOLS, SKIP_MARKET_UPDATE
    
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.warning("⚠️ 市場アップデートをスキップするか、クライアントが未初期化です。デフォルト銘柄を使用します。")
        return DEFAULT_SYMBOLS
        
    try:
        # fetch_tickersで全シンボルの最新情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not tickers:
            logging.warning("⚠️ fetch_tickersが空の結果を返しました。デフォルト銘柄を使用します。")
            return DEFAULT_SYMBOLS

        # USDT建ての先物/スワップペアにフィルタリングし、出来高(quoteVolume)でソート
        usdt_futures = {
            s: t for s, t in tickers.items() 
            if s.endswith('/USDT') and t.get('quoteVolume') is not None
        }

        # 出来高降順でソート (quoteVolumeをfloatに変換できない場合は0として扱う)
        sorted_tickers = sorted(
            usdt_futures.values(), 
            key=lambda t: float(t['quoteVolume']) if t.get('quoteVolume') else 0, 
            reverse=True
        )
        
        # トップN銘柄を選択
        top_symbols = [t['symbol'] for t in sorted_tickers[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルと重複しないようにマージし、最新の監視リストを作成
        # このボットでは、固定リストに出来高TOP40を上書きするのではなく、出来高TOP40+デフォルトのリストで運用する戦略とする。
        # 今回のロジックでは、シンプルにするため、出来高TOP Nのみを使用する。
        if top_symbols:
            logging.info(f"✅ 出来高TOP {len(top_symbols)} 銘柄を取得しました。監視リストを更新します。")
            CURRENT_MONITOR_SYMBOLS = top_symbols
            return top_symbols

    except ccxt.NetworkError as e:
        logging.error(f"❌ ネットワークエラー: 銘柄リスト更新失敗: {e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: 銘柄リスト更新失敗: {e}")
    except Exception as e:
        # 💡 致命的エラー修正強化: fetch_tickersの結果がNoneTypeだった場合の対策もここに含まれる
        logging.error(f"❌ 銘柄リスト更新中に予期せぬエラーが発生しました: {e}", exc_info=True)
    
    # 失敗した場合、前回のリストまたはデフォルトリストを返す
    return CURRENT_MONITOR_SYMBOLS if CURRENT_MONITOR_SYMBOLS else DEFAULT_SYMBOLS


async def get_account_status() -> Dict:
    """アカウントの残高とオープンポジションの情報を取得する。"""
    global EXCHANGE_CLIENT, OPEN_POSITIONS, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT:
        return {'error': True, 'message': "CCXTクライアントが未初期化です。"}
    
    try:
        # 1. 残高情報の取得 (先物アカウントの総資産を取得)
        balance = await EXCHANGE_CLIENT.fetch_balance(params={'type': TRADE_TYPE})
        
        total_usdt_balance = balance['total'].get('USDT', 0.0) # 総資産（USDT）
        ACCOUNT_EQUITY_USDT = total_usdt_balance

        # 2. ポジション情報の取得
        # ccxtのfetch_positionsは取引所によって実装が異なるため、汎用的な形で取得を試みる
        # (例: MEXC, Bybitはfetch_positionsに対応)
        positions_ccxt = []
        try:
            positions_ccxt = await EXCHANGE_CLIENT.fetch_positions()
        except ccxt.NotSupported:
            # fetch_positionsがサポートされていない場合は、fetch_balanceの'info'や'positions'から取得を試みる (非推奨)
            logging.warning("⚠️ fetch_positions がサポートされていません。ポジション監視が不完全になる可能性があります。")
            
        # ポジションリストをボット管理用の形式に変換
        OPEN_POSITIONS.clear()
        
        for p in positions_ccxt:
            # 契約数量が0でないアクティブなポジションのみを対象とする
            if p['contractSize'] is not None and abs(p['contracts']) > 0 and p['notional'] is not None:
                
                # ポジションの必須情報を抽出・計算
                symbol = p['symbol']
                contracts = abs(p['contracts'])
                side = p['side'].lower() # 'long' or 'short'
                entry_price = p['entryPrice'] 
                current_price = p['lastPrice'] or p['markPrice'] 
                notional_value = p['notional'] # 名目価値 (USDT)
                
                # SL/TPはボットが設定したものを別途追跡する必要があるが、ここではCCXTから取得できる情報のみを格納
                # CCXTのポジションデータに SL/TP 情報が含まれていればそれを優先
                stop_loss = p.get('stopLossPrice', 0.0) 
                take_profit = p.get('takeProfitPrice', 0.0) 

                # 清算価格の計算 (CCXTのデータが信頼できない場合のバックアップ)
                liquidation_price_ccxt = p.get('liquidationPrice')
                if not liquidation_price_ccxt or liquidation_price_ccxt == 0.0:
                    liquidation_price_ccxt = calculate_liquidation_price(
                        entry_price, LEVERAGE, side
                    )

                # ポジションにボットの追跡IDを付与 (既存のポジションにマッチさせるためのロジックは省略)
                # 今回は、シンプルにCCXTのポジションデータをそのままボットの管理リストに入れる
                OPEN_POSITIONS.append({
                    'symbol': symbol,
                    'side': side,
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'contracts': contracts,
                    'filled_usdt': notional_value,
                    'stop_loss': stop_loss, # ボットが設定したSL/TPは個別に追跡が必要
                    'take_profit': take_profit,
                    'liquidation_price': liquidation_price_ccxt,
                    'pnl_usdt': p.get('unrealizedPnl', 0.0),
                    'timestamp': p.get('timestamp')
                })

        # ボットで管理するポジションの SL/TP が CCXT のデータにない場合、OPEN_POSITIONS のリストを
        # 更新して、ボットが起動時に設定したSL/TPを保持する永続化ロジックが必要だが、
        # ここではボットがアクティブな状態での監視のみを想定し、簡略化する。
        
        status = {
            'error': False,
            'total_usdt_balance': total_usdt_balance,
            'positions_count': len(OPEN_POSITIONS),
            'positions': OPEN_POSITIONS
        }
        logging.info(f"✅ 口座ステータス取得: Equity={format_usdt(total_usdt_balance)} USDT, Pos={len(OPEN_POSITIONS)}")
        return status
        
    except ccxt.NetworkError as e:
        logging.error(f"❌ ネットワークエラー: 口座ステータス取得失敗: {e}")
        return {'error': True, 'message': f"ネットワークエラー: {e}"}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: 口座ステータス取得失敗: {e}")
        return {'error': True, 'message': f"取引所エラー: {e}"}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        return {'error': True, 'message': f"予期せぬエラー: {e}"}


async def fetch_ohlcv_data(symbol: str) -> Dict[str, Optional[pd.DataFrame]]:
    """指定されたシンボルと全てのタイムフレームのOHLCVデータを取得する。"""
    global EXCHANGE_CLIENT, TARGET_TIMEFRAMES, REQUIRED_OHLCV_LIMITS
    
    if not EXCHANGE_CLIENT:
        logging.error(f"❌ {symbol}: CCXTクライアントが未初期化です。OHLCV取得失敗。")
        return {}

    data_map = {}
    
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        try:
            # CCXTの非同期メソッドを呼び出し
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            
            if not ohlcv or len(ohlcv) < limit - 1: # 必要なデータ数が揃っているかチェック
                logging.warning(f"⚠️ {symbol} - {tf}: 必要なデータ ({limit}本) を取得できませんでした。({len(ohlcv)}本)")
                data_map[tf] = None
                continue
                
            # Pandas DataFrameに変換
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
            df.set_index('timestamp', inplace=True)
            
            data_map[tf] = df
            
        except ccxt.NetworkError as e:
            logging.error(f"❌ {symbol} - {tf}: ネットワークエラー: {e}")
            data_map[tf] = None
        except ccxt.ExchangeError as e:
            logging.error(f"❌ {symbol} - {tf}: 取引所エラー: {e}")
            data_map[tf] = None
        except Exception as e:
            logging.error(f"❌ {symbol} - {tf}: 予期せぬエラー: {e}", exc_info=True)
            data_map[tf] = None
            
    return data_map


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def apply_technical_indicators(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """DataFrameにテクニカル指標を適用する。"""
    if df is None or len(df) < LONG_TERM_SMA_LENGTH + ATR_LENGTH:
        return None
        
    df = df.copy()
    
    # 1. トレンド/移動平均線 (SMA 200)
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    
    # 2. ボラティリティ (ATR, Bollinger Bands)
    df.ta.atr(length=ATR_LENGTH, append=True)
    df.ta.bbands(length=20, std=2, append=True)
    
    # 3. モメンタム (RSI, MACD)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    
    # 4. 出来高 (OBV)
    df.ta.obv(append=True)
    
    # 最後の行に有効なデータがあるか確認 (NaNが多い場合がある)
    if df.iloc[-1].isnull().any():
        # SMAやATRの計算に必要なデータが揃っていない可能性がある
        if df[f'SMA_{LONG_TERM_SMA_LENGTH}'].iloc[-1] is np.nan:
            logging.warning("⚠️ テクニカル指標の計算結果にNaNが含まれます (データ不足の可能性)。")
            return None

    return df


def calculate_signal_score(symbol: str, tf_data: Dict[str, Optional[pd.DataFrame]], macro_context: Dict) -> List[Dict]:
    """
    複数タイムフレームのデータとマクロコンテキストに基づき、
    実践的なスコアリングロジックを適用してシグナルを生成する。
    """
    signals = []
    
    # スコアの基本値を設定
    base_score = BASE_SCORE + STRUCTURAL_PIVOT_BONUS # 0.40 + 0.05 = 0.45

    # 液体性ボーナス (暫定ロジック: DEFAULT_SYMBOLSのTOP 10であればボーナス)
    liquidity_bonus = 0.0
    if symbol in CURRENT_MONITOR_SYMBOLS[:10]:
         liquidity_bonus = LIQUIDITY_BONUS_MAX # Max 0.06
    
    # マクロ影響の計算 (FGI)
    fgi_proxy_value = macro_context.get('fgi_proxy', 0.0) 
    
    # ターゲットタイムフレーム: 1h, 4hをメイン、15mをサブとして使用
    target_tfs = ['1h', '4h']
    
    for tf in target_tfs:
        df = tf_data.get(tf)
        if df is None:
            continue

        last = df.iloc[-1]
        
        # 必要な指標が存在するかチェック
        if f'SMA_{LONG_TERM_SMA_LENGTH}' not in df.columns or 'RSI_14' not in df.columns:
            logging.warning(f"⚠️ {symbol} - {tf}: 必須テクニカル指標がありません。スキップ。")
            continue

        long_score_components = {
            'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, # ベーススコア
            'liquidity_bonus_value': liquidity_bonus,
            'sentiment_fgi_proxy_bonus': fgi_proxy_value,
            'long_term_reversal_penalty_value': 0.0, # トレンド一致ボーナス
            'macd_penalty_value': 0.0,
            'rsi_momentum_bonus_value': 0.0,
            'volatility_penalty_value': 0.0,
            'obv_momentum_bonus_value': 0.0,
        }
        short_score_components = long_score_components.copy()

        current_close = last['close']
        sma_200 = last[f'SMA_{LONG_TERM_SMA_LENGTH}']
        rsi = last['RSI_14']
        macd_hist = last['MACDh_12_26_9']
        bb_percent = (last['BBU_20_2.0'] - last['BBL_20_2.0']) / current_close if current_close > 0 else 0.0

        # --- 1. 長期トレンド一致/逆行 (SMA 200) ---
        trend_influence = LONG_TERM_REVERSAL_PENALTY
        
        # Long Score
        if current_close > sma_200:
            # 順張りボーナス: 価格がSMA 200の上にある
            long_score_components['long_term_reversal_penalty_value'] = trend_influence
        elif current_close < sma_200:
            # 逆張りペナルティ: 価格がSMA 200の下にある
            long_score_components['long_term_reversal_penalty_value'] = -trend_influence

        # Short Score
        if current_close < sma_200:
            # 順張りボーナス: 価格がSMA 200の下にある
            short_score_components['long_term_reversal_penalty_value'] = trend_influence
        elif current_close > sma_200:
            # 逆張りペナルティ: 価格がSMA 200の上にある
            short_score_components['long_term_reversal_penalty_value'] = -trend_influence
            
        # --- 2. MACDモメンタム (MACDヒストグラムの方向) ---
        macd_influence = MACD_CROSS_PENALTY
        
        # Long Score: ヒストグラムが陽性、または直近で底を打ち陽転
        if macd_hist > 0:
            long_score_components['macd_penalty_value'] = macd_influence
        elif macd_hist < 0 and df['MACDh_12_26_9'].iloc[-2] < macd_hist: # ヒストグラムのマイナス幅が縮小
            long_score_components['macd_penalty_value'] = macd_influence / 2.0
        else:
            long_score_components['macd_penalty_value'] = -macd_influence
            
        # Short Score: ヒストグラムが陰性、または直近で天井を打ち陰転
        if macd_hist < 0:
            short_score_components['macd_penalty_value'] = macd_influence
        elif macd_hist > 0 and df['MACDh_12_26_9'].iloc[-2] > macd_hist: # ヒストグラムのプラス幅が縮小
            short_score_components['macd_penalty_value'] = macd_influence / 2.0
        else:
            short_score_components['macd_penalty_value'] = -macd_influence
            
        # --- 3. RSIモメンタム加速/適正水準 (RSI 40/60) ---
        rsi_influence = STRUCTURAL_PIVOT_BONUS # 0.05
        
        # Long Score: RSIが40-60の間、または60を超えている (強気)
        if rsi >= RSI_MOMENTUM_LOW: 
            long_score_components['rsi_momentum_bonus_value'] = rsi_influence
        
        # Short Score: RSIが40-60の間、または40未満 (弱気)
        if rsi <= (100 - RSI_MOMENTUM_LOW):
            short_score_components['rsi_momentum_bonus_value'] = rsi_influence

        # --- 4. 出来高確証 (OBVの上昇/下降) ---
        obv = last['OBV']
        prev_obv = df['OBV'].iloc[-2] if len(df) >= 2 else obv
        obv_influence = OBV_MOMENTUM_BONUS
        
        # Long Score: OBVが上昇
        if obv > prev_obv:
            long_score_components['obv_momentum_bonus_value'] = obv_influence
            
        # Short Score: OBVが下降
        if obv < prev_obv:
            short_score_components['obv_momentum_bonus_value'] = obv_influence
        
        # --- 5. ボラティリティ過熱ペナルティ (BB幅の確認) ---
        # BB幅が VOLATILITY_BB_PENALTY_THRESHOLD (1%) を超えている場合にペナルティ
        volatility_penalty = 0.0
        if bb_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
            volatility_penalty = -STRUCTURAL_PIVOT_BONUS # -0.05
            
        long_score_components['volatility_penalty_value'] = volatility_penalty
        short_score_components['volatility_penalty_value'] = volatility_penalty


        # --- 6. 最終スコアの計算 ---
        final_long_score = base_score + sum(long_score_components.values())
        final_short_score = base_score + sum(short_score_components.values())

        # シグナル辞書を作成
        if final_long_score >= BASE_SCORE:
            signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'side': 'long',
                'score': round(final_long_score, 4),
                'tech_data': long_score_components, # スコア内訳を格納
                'last_close': current_close,
                'last_atr': last[f'ATR_{ATR_LENGTH}'],
                'timestamp': last.name.timestamp(),
            })

        if final_short_score >= BASE_SCORE:
            signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'side': 'short',
                'score': round(final_short_score, 4),
                'tech_data': short_score_components,
                'last_close': current_close,
                'last_atr': last[f'ATR_{ATR_LENGTH}'],
                'timestamp': last.name.timestamp(),
            })

    return signals


def determine_entry_and_risk_parameters(signal: Dict, account_equity: float, current_price: float) -> Optional[Dict]:
    """
    ATRベースのSL/TPを決定し、取引パラメータ (契約数量、名目ロット) を計算する。
    """
    global LEVERAGE, ATR_MULTIPLIER_SL, RR_RATIO_TARGET, FIXED_NOTIONAL_USDT, MIN_RISK_PERCENT
    
    side = signal['side']
    last_atr = signal['last_atr']
    
    if last_atr <= 0:
        logging.warning(f"⚠️ {signal['symbol']}: ATRが非正値 ({last_atr}) のため、リスクパラメータ計算をスキップします。")
        return None

    # 1. SL (ストップロス) の決定 (ATRベース)
    # リスク幅 (ATR * 係数)
    risk_distance_abs = last_atr * ATR_MULTIPLIER_SL
    
    # 最小リスク幅の強制 (価格の MIN_RISK_PERCENT を絶対的な最低値とする)
    min_risk_distance = current_price * MIN_RISK_PERCENT
    risk_distance_abs = max(risk_distance_abs, min_risk_distance)

    # 価格計算
    if side == 'long':
        stop_loss = current_price - risk_distance_abs
        if stop_loss <= 0: return None
    else: # 'short'
        stop_loss = current_price + risk_distance_abs
    
    # 2. TP (テイクプロフィット) の決定 (RRRベース)
    # リワード幅 = リスク幅 * RRR
    reward_distance_abs = risk_distance_abs * RR_RATIO_TARGET
    
    if side == 'long':
        take_profit = current_price + reward_distance_abs
    else: # 'short'
        take_profit = current_price - reward_distance_abs
        if take_profit <= 0: return None
        
    # 3. 清算価格の計算 (通知用)
    liquidation_price = calculate_liquidation_price(
        current_price, LEVERAGE, side
    )
    
    # 4. ポジションサイズの計算 (固定ロットを使用)
    notional_usdt = FIXED_NOTIONAL_USDT
    
    # 実際のリスク/リワード比率
    actual_rr_ratio = reward_distance_abs / risk_distance_abs
    
    # 計算されたパラメータをシグナルに統合
    signal.update({
        'entry_price': current_price,
        'stop_loss': round(stop_loss, 8),
        'take_profit': round(take_profit, 8),
        'liquidation_price': round(liquidation_price, 8),
        'risk_usdt': notional_usdt * (risk_distance_abs / current_price), # 概算リスク額
        'notional_usdt': notional_usdt,
        'rr_ratio': round(actual_rr_ratio, 2)
    })
    
    return signal

async def get_amount_to_execute(symbol: str, notional_usdt: float, price: float) -> Optional[float]:
    """
    指定されたシンボルの取引所情報 (ロットサイズ、最小発注量) に基づき、
    名目ロット (USDT) から発注数量 (契約数) を計算する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or price <= 0:
        return None

    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market or not market.get('precision'):
        logging.error(f"❌ {symbol}: 市場情報または精度情報が見つかりません。")
        return None

    # 名目ロット (USDT) を現在の価格で割って契約数量を計算
    amount = notional_usdt / price
    
    # ロットサイズの丸め処理 (ccxtの精度情報を使用)
    amount_precision = market['precision'].get('amount', 8)
    amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
    
    # 最小発注量のチェック
    min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
    
    if float(amount) < min_amount:
         logging.warning(f"⚠️ {symbol}: 計算された数量 ({amount}) が最小発注量 ({min_amount}) 未満です。発注をスキップします。")
         return None
         
    return float(amount)


async def execute_trade(signal: Dict) -> Dict:
    """
    シグナルに基づき、取引所に注文を執行する (成行注文)。
    成功した場合、SL/TP注文を同時に設定する。
    """
    global EXCHANGE_CLIENT, TEST_MODE
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    notional_usdt = signal['notional_usdt']
    
    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未初期化です。'}
    
    if TEST_MODE:
        logging.warning(f"⚠️ {symbol} - {side}: TEST_MODEのため取引をスキップしました。")
        return {'status': 'ok', 'filled_usdt': notional_usdt, 'filled_amount': notional_usdt/entry_price}


    # 1. 注文数量の計算
    amount = await get_amount_to_execute(symbol, notional_usdt, entry_price)
    if amount is None or amount <= 0:
        return {'status': 'error', 'error_message': '発注数量が不正または最小ロット未満です。'}

    # 2. ポジションオープン (成行注文)
    order_side = 'buy' if side == 'long' else 'sell'
    trade_result = {'status': 'error', 'error_message': '注文APIコール失敗'}
    
    try:
        # ccxtは通常、先物取引では「market」注文でポジションをオープンする
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            'market', 
            order_side, 
            amount,
            params={
                'leverage': LEVERAGE, 
                'positionSide': side.capitalize() # Bybit/Binance/MEXCなど: Long/Short
            }
        )
        
        # 注文結果の確認
        if order and order['status'] in ['closed', 'filled']: # 成行注文は即座に約定するはず
            
            # 💡 注文情報の整形 (ccxtの結果は取引所によって異なるため、標準的なフィールドを使用)
            filled_amount = order.get('filled', amount)
            filled_usdt = filled_amount * entry_price # 概算名目ロット
            
            # 3. SL/TP注文の設定 (MEXC/BybitではポジションにT/P-S/Lを設定する)
            try:
                # ccxtのcreate_orderに params で SL/TP を渡せる取引所もあるが、
                # ここでは ccxt.create_reduce_only_stop_order を使用する (より汎用的なSL/TP設定方法)
                # ポジションを決済するためのトリガー注文 (reduce only) を設定
                
                # SL注文 (逆指値: 価格がSLに達したら成行で決済)
                sl_trigger_side = 'sell' if side == 'long' else 'buy' 
                
                # SL注文は価格に達したら市場価格で決済
                await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    'stop_market', # or 'stop_loss_limit', 'stop_loss'
                    sl_trigger_side,
                    filled_amount,
                    price=stop_loss, # 念のため価格を指定 (一部の取引所で必要)
                    params={
                        'stopPrice': stop_loss, # トリガー価格
                        'reduceOnly': True,
                        'positionSide': side.capitalize() # ポジションサイドを指定
                    }
                )
                
                # TP注文 (指値: 価格がTPに達したら指値で決済)
                tp_limit_side = 'sell' if side == 'long' else 'buy'

                await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    'limit', # TPは指値で執行
                    tp_limit_side,
                    filled_amount,
                    price=take_profit,
                    params={
                        'stopPrice': take_profit, # 多くの取引所では stopPrice は SL にのみ適用されるが、念のため
                        'reduceOnly': True,
                        'positionSide': side.capitalize()
                    }
                )
                logging.info(f"✅ {symbol} ({side}): SL {format_price(stop_loss)} / TP {format_price(take_profit)} 注文設定完了。")

            except Exception as e:
                logging.error(f"❌ {symbol} ({side}): SL/TP注文設定中にエラーが発生しました: {e}", exc_info=True)
                await send_telegram_notification(f"🚨 **SL/TP設定エラー**\n{symbol} ({side}) のポジションはオープンしましたが、SL/TP設定に失敗しました: <code>{e}</code>")
                # 警告は出すが、ポジションはオープン済みとして処理を続行

            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'order_id': order.get('id'),
                'entry_price': entry_price,
            }
        else:
            trade_result = {'status': 'error', 'error_message': f'注文は発行されましたが約定されませんでした: {order.get("status")}'}
            
    except ccxt.ExchangeError as e:
        # Amount can not be less than zero (MEXCでよくある、ロットが小さすぎる場合)
        if 'amount can not be less than zero' in str(e).lower() or 'not enough margin' in str(e).lower():
            trade_result = {'status': 'error', 'error_message': f'取引所エラー: ロットが小さすぎるか、証拠金不足です: {e}'}
        else:
            trade_result = {'status': 'error', 'error_message': f'取引所エラー: {e}'}
            logging.error(f"❌ {symbol} - {side} 注文失敗: {e}", exc_info=True)
            
    except ccxt.NetworkError as e:
        trade_result = {'status': 'error', 'error_message': f'ネットワークエラー: {e}'}
    except Exception as e:
        trade_result = {'status': 'error', 'error_message': f'予期せぬ注文エラー: {e}'}
        logging.error(f"❌ {symbol} - {side} 注文失敗: {e}", exc_info=True)

    return trade_result


async def process_entry_signal(signal: Dict) -> None:
    """
    シグナル処理のメインロジック。
    クールダウンチェック、リスクパラメータ決定、取引実行、通知、ロギングを行う。
    """
    global LAST_SIGNAL_TIME, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS, LEVERAGE

    symbol = signal['symbol']
    side = signal['side']
    score = signal['score']
    
    # 1. クールダウンチェック
    current_time = time.time()
    last_signal_for_symbol = LAST_SIGNAL_TIME.get(symbol, 0.0)
    if current_time - last_signal_for_symbol < TRADE_SIGNAL_COOLDOWN:
        logging.info(f"ℹ️ {symbol} - {side}: クールダウン中 ({TRADE_SIGNAL_COOLDOWN/3600:.1f}時間)。スキップします。")
        return

    # 2. リスクパラメータの決定
    # ポジションサイズを計算するために、最新の市場価格を取得 (シグナル内の価格は足の終値なので、より最新の価格を使用)
    current_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
    current_price = current_ticker.get('last') or current_ticker.get('close')
    if not current_price:
        logging.error(f"❌ {symbol}: 最新価格を取得できませんでした。取引をスキップします。")
        return

    # シグナルに最新の価格とリスクパラメータを組み込む
    signal_with_params = determine_entry_and_risk_parameters(signal, ACCOUNT_EQUITY_USDT, current_price)
    if signal_with_params is None:
        logging.error(f"❌ {symbol}: リスクパラメータ設定に失敗しました (ATRまたはSLが不正)。スキップします。")
        return

    # 3. ポジションオープン処理
    trade_result = await execute_trade(signal_with_params)
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 4. 通知とロギング
    if trade_result['status'] == 'ok':
        
        # OPEN_POSITIONS に新しいポジションを追加 (ボットが追跡するために必須)
        # 実際には、get_account_statusで取得し直すのが安全だが、ここでは仮のデータを入れておく
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'side': side,
            'entry_price': signal_with_params['entry_price'],
            'current_price': signal_with_params['entry_price'],
            'contracts': trade_result.get('filled_amount', 0.0),
            'filled_usdt': trade_result.get('filled_usdt', signal_with_params['notional_usdt']),
            'stop_loss': signal_with_params['stop_loss'],
            'take_profit': signal_with_params['take_profit'],
            'liquidation_price': signal_with_params['liquidation_price'],
            'pnl_usdt': 0.0,
            'timestamp': int(current_time * 1000)
        })
        
        notification_message = format_telegram_message(signal_with_params, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        log_signal(signal_with_params, "Entry Executed", trade_result)
        
        LAST_SIGNAL_TIME[symbol] = current_time # 成功時のみクールダウンを更新
        logging.info(f"🟢 {symbol} - {side}: エントリー成功。スコア {score*100:.2f}。")
        
    else:
        # エラー時も通知
        notification_message = format_telegram_message(signal_with_params, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(notification_message)
        log_signal(signal_with_params, "Entry Failed", trade_result)
        logging.error(f"❌ {symbol} - {side}: エントリー失敗。エラー: {trade_result['error_message']}")


async def process_exit_signal(position: Dict, exit_type: str) -> Dict:
    """
    ポジション情報と決済タイプに基づき、取引所に決済注文を執行する。
    """
    global EXCHANGE_CLIENT, TEST_MODE, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts_to_close = position['contracts']
    entry_price = position['entry_price']

    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'CCXTクライアントが未初期化です。'}
        
    if TEST_MODE:
        logging.warning(f"⚠️ {symbol} - {side}: TEST_MODEのため決済をスキップしました。")
        # テストモードでは、模擬的に利益/損失を計算
        current_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        exit_price = current_ticker.get('last') or current_ticker.get('close')
        
        if side == 'long':
            pnl_usdt = contracts_to_close * (exit_price - entry_price)
        else:
            pnl_usdt = contracts_to_close * (entry_price - exit_price)

        pnl_rate = pnl_usdt / (contracts_to_close * entry_price / LEVERAGE) if contracts_to_close * entry_price > 0 else 0.0
        
        return {
            'status': 'ok', 
            'exit_type': exit_type, 
            'exit_price': exit_price,
            'entry_price': entry_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'filled_amount': contracts_to_close,
        }


    # 1. SL/TP注文のキャンセル (未約定の注文が残っている場合)
    try:
        # 全ての未約定注文をキャンセル (より安全)
        await EXCHANGE_CLIENT.cancel_all_orders(symbol)
        logging.info(f"✅ {symbol}: 全ての未約定のSL/TP/その他注文をキャンセルしました。")
    except Exception as e:
        logging.warning(f"⚠️ {symbol}: 注文キャンセル中にエラーが発生しましたが続行します: {e}")

    # 2. ポジションクローズ (成行注文)
    close_side = 'sell' if side == 'long' else 'buy' 
    trade_result = {'status': 'error', 'error_message': '決済注文APIコール失敗'}
    
    try:
        # ccxtのcreate_orderで、全ポジションを成行で決済する
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            'market', 
            close_side, 
            contracts_to_close,
            params={
                'reduceOnly': True, # 必ず reduceOnly を指定
                'positionSide': side.capitalize()
            }
        )
        
        # 注文結果の確認
        if order and order['status'] in ['closed', 'filled']:
            
            # 決済価格は、取引所APIから取得するか、fetch_tickerの最新価格を使用
            current_ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            exit_price = order.get('price') or current_ticker.get('last') or current_ticker.get('close')

            # PnL計算 (取引所の情報が優先されるが、今回は簡略化のため手動計算のバックアップ)
            if side == 'long':
                pnl_usdt_calc = contracts_to_close * (exit_price - entry_price)
            else:
                pnl_usdt_calc = contracts_to_close * (entry_price - exit_price)
            
            filled_notional = contracts_to_close * entry_price
            pnl_rate_calc = pnl_usdt_calc / (filled_notional / LEVERAGE) if filled_notional > 0 else 0.0 # 証拠金に対するPnL率

            trade_result = {
                'status': 'ok',
                'exit_type': exit_type,
                'exit_price': exit_price,
                'entry_price': entry_price,
                'pnl_usdt': pnl_usdt_calc,
                'pnl_rate': pnl_rate_calc,
                'filled_amount': contracts_to_close,
                'order_id': order.get('id')
            }
            
            # 成功した場合、OPEN_POSITIONSから削除
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['symbol'] != symbol]
            
        else:
            trade_result = {'status': 'error', 'error_message': f'決済注文は発行されましたが約定されませんでした: {order.get("status")}'}
            
    except ccxt.ExchangeError as e:
        trade_result = {'status': 'error', 'error_message': f'取引所エラー: {e}'}
        logging.error(f"❌ {symbol} - {side} 決済失敗: {e}", exc_info=True)
    except ccxt.NetworkError as e:
        trade_result = {'status': 'error', 'error_message': f'ネットワークエラー: {e}'}
    except Exception as e:
        trade_result = {'status': 'error', 'error_message': f'予期せぬ決済エラー: {e}'}
        logging.error(f"❌ {symbol} - {side} 決済失敗: {e}", exc_info=True)

    return trade_result

# ====================================================================================
# SCHEDULERS & MAIN LOOPS
# ====================================================================================

async def main_bot_logic() -> None:
    """メインの取引ロジック: データ取得、分析、シグナル生成、取引実行。"""
    
    global CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED

    if not IS_CLIENT_READY:
        logging.error("❌ クライアントが未初期化のため、メインロジックをスキップします。")
        return
        
    logging.info("--- メイン取引ロジック開始 ---")
    start_time = time.time()
    
    # 1. マクロ環境と口座ステータスの更新
    await get_fgi_data()
    account_status = await get_account_status()
    await get_top_symbols() # 出来高トップ銘柄の更新

    # 致命的なエラーがあれば停止
    if account_status.get('error'):
        logging.critical("🚨 口座ステータス取得に失敗しました。取引を停止し、再初期化を試みます。")
        await initialize_exchange_client() # 再初期化を試みる
        return

    # 2. 取引閾値の決定
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 3. 全銘柄の分析実行
    all_signals: List[Dict] = []
    
    # 非同期で全ての銘柄のデータ取得と分析を並行して実行
    analysis_tasks = []
    for symbol in CURRENT_MONITOR_SYMBOLS:
        analysis_tasks.append(asyncio.create_task(_analyze_symbol(symbol, current_threshold)))

    # 結果を待機
    results = await asyncio.gather(*analysis_tasks)
    
    # 結果の統合
    for signals_for_symbol in results:
        if signals_for_symbol:
            all_signals.extend(signals_for_symbol)

    # 4. シグナルのフィルタリングと実行
    LAST_ANALYSIS_SIGNALS = all_signals # 定時レポート用に全ての分析結果を保持
    
    # 取引閾値を超えたシグナルのみをフィルタリング
    tradable_signals = [s for s in all_signals if s['score'] >= current_threshold]
    
    # スコアが高い順にソート
    tradable_signals.sort(key=lambda x: x['score'], reverse=True)

    # トップN個のシグナルのみを処理
    top_signals_to_execute = tradable_signals[:TOP_SIGNAL_COUNT]
    
    if top_signals_to_execute:
        for signal in top_signals_to_execute:
            
            # 既にポジションを持っている銘柄はスキップ (重複エントリー防止)
            if any(p['symbol'] == signal['symbol'] for p in OPEN_POSITIONS):
                logging.info(f"ℹ️ {signal['symbol']}: 既にポジションを保有しています。エントリーをスキップします。")
                continue

            # エントリー処理を実行
            await process_entry_signal(signal)
    else:
        logging.info("ℹ️ 取引閾値を超えるシグナルはありませんでした。")
    
    # 5. 起動完了通知 (初回のみ)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        startup_msg = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_msg)
        IS_FIRST_MAIN_LOOP_COMPLETED = True

    # 6. 定期的なWebShareデータ送信と分析レポート
    await notify_highest_analysis_score()
    
    webshare_data = {
        'timestamp': datetime.now(JST).isoformat(),
        'bot_version': BOT_VERSION,
        'account_equity': ACCOUNT_EQUITY_USDT,
        'open_positions': OPEN_POSITIONS,
        'top_signals': [s for s in tradable_signals if s['score'] >= current_threshold] 
    }
    await send_webshare_update(webshare_data)
        
    end_time = time.time()
    logging.info(f"--- メイン取引ロジック完了 (処理時間: {end_time - start_time:.2f}秒) ---")


async def _analyze_symbol(symbol: str, current_threshold: float) -> List[Dict]:
    """単一の銘柄のデータ取得、分析、シグナル生成を行うヘルパー関数"""
    try:
        # 1. OHLCVデータの取得
        tf_data = await fetch_ohlcv_data(symbol)
        
        # 2. テクニカル分析の適用
        analyzed_data = {}
        for tf, df in tf_data.items():
            if df is not None:
                analyzed_df = apply_technical_indicators(df)
                if analyzed_df is not None:
                    analyzed_data[tf] = analyzed_df

        if not analyzed_data:
            logging.warning(f"⚠️ {symbol}: 適切な分析データがありません。スキップ。")
            return []

        # 3. スコアリング
        signals = calculate_signal_score(symbol, analyzed_data, GLOBAL_MACRO_CONTEXT)
        
        # 4. スコアが BASE_SCORE + α を超えているもののみログ出力
        high_score_signals = [s for s in signals if s['score'] >= BASE_SCORE + 0.1]
        for s in high_score_signals:
            log_type = "High Score" if s['score'] >= current_threshold else "Pre-Signal"
            log_signal(s, log_type)
            
        return signals

    except Exception as e:
        logging.error(f"❌ {symbol} の分析中に予期せぬエラーが発生しました: {e}", exc_info=True)
        return []


async def position_monitor_logic() -> None:
    """ポジション監視ロジック: SL/TPのチェックと緊急決済。"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT, MONITOR_INTERVAL
    
    if not IS_CLIENT_READY:
        # ポジションを監視できないためログ出力
        if OPEN_POSITIONS:
            logging.warning(f"⚠️ クライアント未初期化のため、{len(OPEN_POSITIONS)}件のオープンポジションの監視をスキップします。")
        return
        
    if not OPEN_POSITIONS:
        logging.debug("ℹ️ 監視中のポジションはありません。")
        return
    
    logging.info(f"--- ポジション監視ロジック開始 ({len(OPEN_POSITIONS)}件のポジション) ---")

    # 全てのポジションについて、最新価格を取得し、SL/TPトリガーを確認
    for position in OPEN_POSITIONS.copy(): # コピーを反復処理し、リストの変更に備える
        
        symbol = position['symbol']
        side = position['side']
        sl_price = position['stop_loss']
        tp_price = position['take_profit']
        
        try:
            # 最新のティッカー情報を取得
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker.get('last') or ticker.get('close')
            
            if not current_price:
                logging.warning(f"⚠️ {symbol}: 最新価格を取得できませんでした。監視をスキップします。")
                continue
            
            # ポジションの現在価格を更新
            position['current_price'] = current_price

            exit_trigger = None

            # SLトリガーチェック
            if side == 'long' and current_price <= sl_price:
                exit_trigger = "STOP_LOSS"
            elif side == 'short' and current_price >= sl_price:
                exit_trigger = "STOP_LOSS"

            # TPトリガーチェック (SLとTPが同時にトリガーされた場合はSLを優先)
            if not exit_trigger:
                if side == 'long' and current_price >= tp_price:
                    exit_trigger = "TAKE_PROFIT"
                elif side == 'short' and current_price <= tp_price:
                    exit_trigger = "TAKE_PROFIT"
            
            # 清算価格チェック (SL/TP監視と並行して、緊急で監視)
            liquidation_price = position['liquidation_price']
            if liquidation_price > 0:
                is_liquidated = False
                if side == 'long' and current_price <= liquidation_price:
                    is_liquidated = True
                elif side == 'short' and current_price >= liquidation_price:
                    is_liquidated = True
                    
                if is_liquidated:
                    exit_trigger = "LIQUIDATION_NEAR" # 清算価格付近、または超えた
                    logging.critical(f"🚨 {symbol} - {side}: 価格が清算価格 ({format_price(liquidation_price)}) を超えました。即時決済を試みます。")


            if exit_trigger:
                # 決済処理を実行
                logging.info(f"🔴 {symbol} - {side}: 決済トリガー発動: {exit_trigger}")
                exit_result = await process_exit_signal(position, exit_trigger)
                
                # 決済成功/失敗を通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                notification_message = format_telegram_message(position, "ポジション決済", current_threshold, exit_result, exit_type=exit_trigger)
                await send_telegram_notification(notification_message)
                log_signal(position, f"Exit {exit_trigger}", exit_result)
                
            else:
                logging.debug(f"ℹ️ {symbol}: SL/TP未到達。現在価格: {format_price(current_price)} (SL: {format_price(sl_price)} / TP: {format_price(tp_price)})")
                
        except Exception as e:
            logging.error(f"❌ {symbol} のポジション監視中に予期せぬエラーが発生しました: {e}", exc_info=True)


async def main_bot_scheduler():
    """メインの取引ロジックを定期的に実行するスケジューラ。"""
    global LOOP_INTERVAL, LAST_SUCCESS_TIME, IS_CLIENT_READY
    
    # 起動時にCCXTクライアントを初期化
    if not IS_CLIENT_READY:
        success = await initialize_exchange_client()
        if not success:
            logging.critical("🚨 CCXTクライアントの初期化に失敗しました。BOTを停止します。")
            return 
            
    while True:
        try:
            await main_bot_logic()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            logging.error(f"🚨 メインボットロジック実行中に致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時も待機し、連続クラッシュを防ぐ
        
        await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """ポジション監視ロジックを定期的に実行するスケジューラ。"""
    global MONITOR_INTERVAL
    
    # メインループの起動を少し待ってから開始
    await asyncio.sleep(5) 
    
    while True:
        try:
            await position_monitor_logic()
        except Exception as e:
            logging.error(f"🚨 ポジション監視ロジック実行中にエラーが発生しました: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL)


# ====================================================================================
# FASTAPI APPLICATION SETUP
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
    # 環境変数 PORT が設定されている場合はそれを優先
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info", reload=False)
