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
# 6. 💡 修正: format_telegram_message内の 'NoneType' object has no attribute 'get' エラーを修正
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
        # 💡 【修正: NoneTypeエラー対策】 trade_resultがNoneの場合は、get()を呼び出さずにエラーメッセージを生成
        elif trade_result is None:
            trade_status_line = f"❌ **自動売買 失敗**: 取引結果オブジェクトの取得に失敗 ('NoneType' object)"
        # 💡 【修正: 元のエラー処理】 trade_resultがDictionaryであり、APIエラーの場合
        elif trade_result.get('status') == 'error':
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
                })
                
        # グローバル変数に保存
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"✅ オープンポジション ({len(OPEN_POSITIONS)}件) の情報を取得しました。")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 取引所エラー: {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - ネットワークエラー: {e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定されたシンボルのOHLCVデータをCCXTから取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCVデータ取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    # CCXTのシンボルに変換 (例: BTC/USDT -> BTC/USDT:USDT)
    ccxt_symbol = symbol 
    if EXCHANGE_CLIENT.id == 'mexc':
        # MEXCの場合、先物はシンボルにクォート通貨を付ける必要がある (例: BTC/USDT:USDT)
        base = symbol.split('/')[0]
        quote = symbol.split('/')[1]
        if EXCHANGE_CLIENT.markets.get(f"{base}/{quote}:{quote}"):
            ccxt_symbol = f"{base}/{quote}:{quote}"
        elif EXCHANGE_CLIENT.markets.get(symbol):
            ccxt_symbol = symbol
        else:
            logging.error(f"❌ MEXC: {symbol} の先物シンボルが見つかりません。")
            return None


    try:
        # fetch_ohlcvを非同期で呼び出す
        ohlcv_data = await EXCHANGE_CLIENT.fetch_ohlcv(
            ccxt_symbol, 
            timeframe, 
            limit=limit
        )
        
        if not ohlcv_data or len(ohlcv_data) < limit:
            logging.warning(f"⚠️ OHLCVデータが不足しています ({len(ohlcv_data)}/{limit}本)。 {symbol}/{timeframe}")
            # データが不足している場合でも、取得できたデータフレームを返す
            
        df = pd.DataFrame(
            ohlcv_data, 
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        # タイムスタンプをUTCのdatetimeに変換し、インデックスに設定
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
        df = df.set_index('datetime')
        
        logging.info(f"✅ OHLCVデータ取得完了 ({symbol}/{timeframe} - {len(df)}本)")
        return df

    except ccxt.ExchangeError as e:
        logging.error(f"❌ OHLCVデータ取得失敗 - 取引所エラー: {e} ({symbol}/{timeframe})")
    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCVデータ取得失敗 - ネットワークエラー: {e} ({symbol}/{timeframe})")
    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得失敗 - 予期せぬエラー: {e} ({symbol}/{timeframe})", exc_info=True)
        
    return None

async def fetch_available_symbols() -> List[str]:
    """取引所からアクティブな先物取引シンボルのリストを取得する"""
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 監視シンボルリスト取得失敗: CCXTクライアントが準備できていません。デフォルトリストを使用します。")
        return DEFAULT_SYMBOLS
        
    try:
        # 市場情報の取得
        markets = await EXCHANGE_CLIENT.load_markets()
        
        # USDT建ての先物/スワップシンボルをフィルタリング
        future_symbols = []
        for symbol, market in markets.items():
            if market.get('quote') == 'USDT' and market.get('type') in ['future', 'swap'] and market.get('active', False):
                future_symbols.append(symbol)
                
        # 出来高ベースでTOP Nを絞り込む (fetch_tickersの成功に依存)
        
        # fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if tickers:
            # 24時間出来高 (quoteVolume) でソート
            # NOTE: MEXCの場合、'quoteVolume' は 'amount' (出来高) の金額換算値だが、他の取引所では異なる場合がある。
            # ここでは一般的な 'quoteVolume' を使用
            volume_tickers = {s: t.get('quoteVolume', 0) for s, t in tickers.items() if s in future_symbols and t.get('quoteVolume') is not None}
            
            # 出来高の降順でソート
            sorted_by_volume = sorted(volume_tickers.items(), key=lambda item: item[1], reverse=True)
            
            # TOP N シンボルを取得し、DEFAULT_SYMBOLSとマージ (DEFAULT_SYMBOLSを優先)
            top_symbols = [s for s, v in sorted_by_volume if s.endswith('/USDT')][:TOP_SYMBOL_LIMIT]
            
            # デフォルトシンボルとTOPシンボルをマージ（重複なし）
            final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
            
            logging.info(f"✅ 出来高 TOP {TOP_SYMBOL_LIMIT} + Default ({len(DEFAULT_SYMBOLS)}件) のシンボルをマージし、監視シンボルリスト ({len(final_symbols)}件) を更新しました。")
            CURRENT_MONITOR_SYMBOLS = final_symbols
            return final_symbols
            
        else:
            logging.warning("⚠️ fetch_tickersが失敗したか、出来高情報がないため、デフォルトの監視リストを使用します。")
            return DEFAULT_SYMBOLS

    except Exception as e:
        logging.error(f"❌ 監視シンボルリスト取得中に予期せぬエラー: {e}", exc_info=True)
        return DEFAULT_SYMBOLS


async def fetch_fgi_context() -> float:
    """Fear & Greed Index (FGI) を取得し、-0.05から+0.05の間のマクロスコアに変換する"""
    global GLOBAL_MACRO_CONTEXT
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            fgi_data = data['data'][0]
            value_str = fgi_data.get('value', '50')
            value = int(value_str)
            
            # FGI (0=Extreme Fear, 100=Extreme Greed) を -0.05 から +0.05 の範囲に正規化
            # 50 (Neutral) -> 0.0
            # 0 (Extreme Fear) -> -0.05
            # 100 (Extreme Greed) -> +0.05
            fgi_proxy = (value - 50) / 1000  # (0-100) -> (-50 to +50) / 1000 = (-0.05 to +0.05)
            
            fgi_proxy = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_proxy))
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({fgi_data.get('value_classification', 'N/A')})"
            
            logging.info(f"✅ FGIマクロコンテキストを更新: Raw={value}, Proxy={fgi_proxy:+.4f}")
            return fgi_proxy
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI APIからマクロコンテキスト取得失敗: {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータ処理中にエラーが発生しました: {e}", exc_info=True)
        
    # 失敗時は0.0を返す
    GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
    GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = 'N/A (Failed)'
    return 0.0


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def apply_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCVデータにテクニカル指標を適用する"""
    if df is None or df.empty:
        return df
        
    # SMA (長期トレンド)
    df['SMA_LONG'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # ATR (ボラティリティ/リスク管理)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # RSI (モメンタム/売られすぎ・買われすぎ)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # MACD (短期モメンタム)
    macd_result = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = df.join(macd_result) # MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
    
    # Bollinger Bands (ボラティリティ/過熱度)
    bb_result = ta.bbands(df['close'], length=20, std=2.0)
    df = df.join(bb_result) # BBL_20_2.0, BBM_20_2.0, BBU_20_2.0, BBB_20_2.0, BBP_20_2.0
    
    # OBV (出来高モメンタム)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # トレンドの傾き (過去数本の傾きを見る)
    df['CLOSE_SLOPE'] = ta.slope(df['close'], length=5) 
    
    # NaNを削除（指標計算のために過去データが必要なため、前半のNaN行はカット）
    df = df.dropna()
    
    return df

def calculate_signal_score(symbol: str, df: pd.DataFrame, side: str, macro_context: Dict) -> Tuple[float, Dict]:
    """
    指定されたデータフレームと取引方向に基づき、シグナルスコアを計算する。
    スコアはBASE_SCORE (0.40)から始まり、各種要素を加算・減算する。
    """
    if df is None or df.empty:
        return 0.0, {}
        
    last = df.iloc[-1]
    
    # 1. ベーススコア
    score = BASE_SCORE # 0.40
    tech_data = {'base_score': BASE_SCORE}
    
    # 2. 構造的優位性 (エントリー条件)
    # ここでは単純な優位性ボーナスとして常に加算するが、実運用ではエントリー条件に合格した場合に加算
    struct_bonus = STRUCTURAL_PIVOT_BONUS
    score += struct_bonus
    tech_data['structural_pivot_bonus'] = struct_bonus
    
    # 3. 長期トレンドとの方向性一致 (順張りボーナス/逆張りペナルティ)
    # 終値 vs SMA_LONG (200期間) の関係
    long_term_reversal_penalty_value = 0.0
    is_above_sma = last['close'] > last['SMA_LONG']
    is_below_sma = last['close'] < last['SMA_LONG']
    
    if side == 'long':
        if is_above_sma:
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        elif is_below_sma:
            long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    elif side == 'short':
        if is_below_sma:
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        elif is_above_sma:
            long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
            
    score += long_term_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # 4. MACDモメンタムとの一致 (短期トレンド確証)
    macd_penalty_value = 0.0
    
    # MACD LineがSignal Lineの上で、かつHistogramがプラス (強いロングモメンタム)
    is_strong_long_macd = last['MACD_12_26_9'] > last['MACDs_12_26_9'] and last['MACDh_12_26_9'] > 0
    # MACD LineがSignal Lineの下で、かつHistogramがマイナス (強いショートモメンタム)
    is_strong_short_macd = last['MACD_12_26_9'] < last['MACDs_12_26_9'] and last['MACDh_12_26_9'] < 0
    
    if side == 'long':
        if is_strong_long_macd:
            macd_penalty_value = MACD_CROSS_PENALTY # モメンタム一致ボーナス
        elif is_strong_short_macd:
            macd_penalty_value = -MACD_CROSS_PENALTY # モメンタム逆行ペナルティ
    elif side == 'short':
        if is_strong_short_macd:
            macd_penalty_value = MACD_CROSS_PENALTY # モメンタム一致ボーナス
        elif is_strong_long_macd:
            macd_penalty_value = -MACD_CROSS_PENALTY # モメンタム逆行ペナルティ
            
    score += macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # 5. RSIモメンタム加速/適正水準 (RSI > 40 Long / RSI < 60 Short)
    rsi_momentum_bonus_value = 0.0
    if side == 'long' and last['RSI'] > RSI_MOMENTUM_LOW:
        # Longシグナルの場合、RSIが40以上であればモメンタムがあると見なす
        rsi_momentum_bonus_value = 0.5 * (last['RSI'] - RSI_MOMENTUM_LOW) / (100 - RSI_MOMENTUM_LOW) * (MACD_CROSS_PENALTY * 2) # 最大でMACDペナルティの2倍
        rsi_momentum_bonus_value = min(MACD_CROSS_PENALTY * 2, rsi_momentum_bonus_value) # 上限設定
    elif side == 'short' and last['RSI'] < (100 - RSI_MOMENTUM_LOW):
        # Shortシグナルの場合、RSIが60以下であればモメンタムがあると見なす
        rsi_momentum_bonus_value = 0.5 * ((100 - RSI_MOMENTUM_LOW) - last['RSI']) / (100 - RSI_MOMENTUM_LOW) * (MACD_CROSS_PENALTY * 2) 
        rsi_momentum_bonus_value = min(MACD_CROSS_PENALTY * 2, rsi_momentum_bonus_value) # 上限設定
        
    score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    # 6. OBVのモメンタム確証 (出来高による価格変動の確証)
    obv_momentum_bonus_value = 0.0
    if side == 'long' and last['OBV'] > last['OBV_SMA']:
        # ロングでOBVがSMAを上回っていれば、出来高が上昇をサポートしている
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
    elif side == 'short' and last['OBV'] < last['OBV_SMA']:
        # ショートでOBVがSMAを下回っていれば、出来高が下落をサポートしている
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # 7. ボラティリティ過熱度によるペナルティ
    volatility_penalty_value = 0.0
    
    # BB Width (BBB) / Close Price の比率でボラティリティの過熱度を判定
    # BBB (バンド幅のパーセント表示)
    bb_width_ratio = last['BBB_20_2.0'] / 100 
    
    if bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD: # BB幅が価格の1%を超えている場合
        # 過熱度に応じてペナルティを課す (上限は-0.10)
        # BB_WIDTH_RATIOが0.02 (2%)で最大のペナルティになるように調整
        penalty_factor = min(1.0, (bb_width_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) / VOLATILITY_BB_PENALTY_THRESHOLD)
        volatility_penalty_value = -0.10 * penalty_factor
        
    score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # 8. マクロ/センチメント影響ボーナス (FGI)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = 0.0
    
    if side == 'long' and fgi_proxy > 0:
        sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, fgi_proxy)
    elif side == 'short' and fgi_proxy < 0:
        sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, abs(fgi_proxy)) # ショートの場合はマイナス値を絶対値で評価
    
    # ショート/ロングの方向に応じてマクロボーナスを調整
    if side == 'long':
        sentiment_fgi_proxy_bonus = fgi_proxy
    elif side == 'short':
        sentiment_fgi_proxy_bonus = -fgi_proxy
        
    # 影響をFGI_PROXY_BONUS_MAXの範囲に限定
    if abs(sentiment_fgi_proxy_bonus) > FGI_PROXY_BONUS_MAX:
        sentiment_fgi_proxy_bonus = FGI_PROXY_BONUS_MAX if sentiment_fgi_proxy_bonus > 0 else -FGI_PROXY_BONUS_MAX
        
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # 9. 流動性ボーナス (TOP_SYMBOL_LIMIT に入っていればボーナス)
    liquidity_bonus_value = 0.0
    # symbolは CCXTの標準形式 (BTC/USDT) で渡ってくることを前提
    symbol_base = symbol.split('/')[0] if '/' in symbol else symbol
    
    if symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]:
        # TOP_SYMBOL_LIMITに入る銘柄であれば、流動性ボーナスを与える
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # 最終的なスコアを 0.0 から 1.0 の間にクリップ
    final_score = max(0.0, min(1.0, score))
    
    return final_score, tech_data


def calculate_risk_and_targets(df: pd.DataFrame, side: str, risk_tolerance_usdt: float) -> Tuple[float, float, float, float, float]:
    """
    ATRに基づき、SL/TP価格とリスクリワード比率を計算する。
    
    戻り値:
        Tuple[entry_price, stop_loss, take_profit, risk_usdt, rr_ratio]
    """
    if df is None or df.empty:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    last = df.iloc[-1]
    entry_price = last['close']
    atr_value = last['ATR']
    
    # ATRに基づいたSL幅
    sl_width_abs = atr_value * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (MIN_RISK_PERCENTに基づく絶対値)
    min_risk_abs = entry_price * MIN_RISK_PERCENT
    
    # 採用するSL幅は、ATRベースと最低リスク幅の大きい方
    sl_width = max(sl_width_abs, min_risk_abs)

    if side == 'long':
        stop_loss = entry_price - sl_width
        take_profit = entry_price + (sl_width * RR_RATIO_TARGET)
    else: # short
        stop_loss = entry_price + sl_width
        take_profit = entry_price - (sl_width * RR_RATIO_TARGET)
        
    # SL/TPが0以下にならないように保証
    stop_loss = max(0.000001, stop_loss)
    take_profit = max(0.000001, take_profit)
    
    # 最終的なリスクリワード比率 (RR_RATIO_TARGETに等しいはず)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    rr_ratio = reward_width / risk_width if risk_width > 0 else 0.0
    
    # ロットサイズの計算: 固定ロットを採用するため、リスク額は固定ロットから逆算
    # ここでは固定ロットのまま計算を続行し、risk_usdtも固定ロットから逆算する
    notional_usdt = FIXED_NOTIONAL_USDT
    
    # 許容リスク額 (USDT)
    # Risk = Notional_USDT * (SL_Ratio) * Leverage
    sl_ratio = risk_width / entry_price 
    risk_usdt = notional_usdt * sl_ratio * LEVERAGE
    
    # 💥 (将来の拡張: ここでロットサイズをリスクベースで計算し、notional_usdtを更新する)
    
    logging.debug(f"ℹ️ {side} - Entry:{entry_price:.4f}, ATR:{atr_value:.4f}, SL_Width_ABS:{sl_width_abs:.4f}, SL_Width:{sl_width:.4f}, Risk_USDT:{risk_usdt:.2f}, RRR:{rr_ratio:.2f}")

    return entry_price, stop_loss, take_profit, risk_usdt, rr_ratio


# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

async def execute_trade(signal_data: Dict) -> Dict:
    """
    CCXTクライアントを介して取引を実行し、結果を返す。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if TEST_MODE:
        logging.warning("⚠️ TEST_MODEが有効です。取引は実行されません。")
        return {
            'status': 'ok',
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'filled_amount': FIXED_NOTIONAL_USDT / signal_data['entry_price'], 
            'message': "TEST_MODE: 取引シミュレーション成功",
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    symbol = signal_data['symbol']
    side = signal_data['side']
    entry_price = signal_data['entry_price']
    stop_loss = signal_data['stop_loss']
    take_profit = signal_data['take_profit']
    
    # CCXTのシンボルに変換 (例: BTC/USDT -> BTC/USDT:USDT)
    ccxt_symbol = symbol 
    if EXCHANGE_CLIENT.id == 'mexc':
        base = symbol.split('/')[0]
        quote = symbol.split('/')[1]
        ccxt_symbol = f"{base}/{quote}:{quote}"
        
    # 注文に必要な量を計算
    try:
        # 固定ロット (USDT) を使用
        notional_usdt = FIXED_NOTIONAL_USDT
        amount_to_trade = notional_usdt / entry_price 
        
        # 注文方向
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 注文タイプ (ここでは成行注文)
        order_type = 'market'
        
        # 注文の執行 (成行注文)
        order = await EXCHANGE_CLIENT.create_order(
            ccxt_symbol,
            order_type,
            order_side,
            amount_to_trade,
            params={
                # MEXCの場合、ポジションのオープンタイプとポジションタイプが必要
                'openType': 2, # 2=Cross Margin
                'positionType': 1 if side == 'long' else 2, # 1=Long, 2=Short
            }
        )
        
        # 約定情報の確認
        if order.get('status') in ['closed', 'ok'] or order.get('info', {}).get('status') == 'FILLED':
            # 注文が約定したとして、約定量と約定価格を取得
            filled_amount = order.get('filled', amount_to_trade)
            filled_price = order.get('average', entry_price)
            filled_notional_usdt = filled_amount * filled_price
            
            # 注文の成功ログ
            logging.info(f"✅ 取引実行成功: {symbol} {side} {filled_amount:.4f} @ {filled_price:.4f} (Notional: {format_usdt(filled_notional_usdt)})")

            # ポジションのクローズに必要なIDなどを取得
            # 💡 MEXCではポジションを特定するための vol/position_id などが必要になる場合がある
            # ここではシンプルに、約定情報とSL/TP価格を返す

            return {
                'status': 'ok',
                'order_id': order.get('id'),
                'filled_amount': filled_amount,
                'filled_price': filled_price,
                'filled_usdt': filled_notional_usdt,
                'entry_price': filled_price, # エントリー価格を更新
                'contracts': filled_amount,
                'message': '注文が正常に約定しました。',
            }
        else:
            error_msg = f"注文が約定しませんでした: Status={order.get('status', 'N/A')}, Info={order.get('info')}"
            logging.error(f"❌ 取引実行失敗: {error_msg}")
            return {'status': 'error', 'error_message': error_msg}


    except ccxt.InsufficientFunds as e:
        error_msg = f"証拠金不足エラー: {e}"
        logging.error(f"❌ 取引実行失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.InvalidOrder as e:
        error_msg = f"無効な注文エラー (ロットサイズ or 価格問題): {e}"
        logging.error(f"❌ 取引実行失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except ccxt.NetworkError as e:
        error_msg = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引実行失敗: {error_msg}")
        return {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬ取引エラー: {e}"
        logging.error(f"❌ 取引実行失敗: {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg}


async def manage_sl_tp(symbol: str, side: str, contracts: float, entry_price: float, stop_loss: float, take_profit: float, trade_result: Dict) -> Dict:
    """
    ポジションに対してSL/TP注文を設定する。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        logging.warning("⚠️ TEST_MODEが有効です。SL/TP注文は実行されません。")
        return {'status': 'ok', 'message': 'TEST_MODE: SL/TP設定シミュレーション成功'}

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    ccxt_symbol = symbol 
    if EXCHANGE_CLIENT.id == 'mexc':
        base = symbol.split('/')[0]
        quote = symbol.split('/')[1]
        ccxt_symbol = f"{base}/{quote}:{quote}"

    # SL/TP注文の方向 (ポジションと逆)
    close_side = 'sell' if side == 'long' else 'buy'
    
    # MEXCは複合注文 (TP/SL) がサポートされていない場合があるため、個別に発注を試みる

    # 1. Stop Loss (ストップ指値) 注文
    sl_order_result = {'status': 'N/A'}
    try:
        # Stop Lossは、ストップ価格に達したら成行でクローズする 'Stop Market' 注文として発注
        sl_order_result = await EXCHANGE_CLIENT.create_order(
            ccxt_symbol,
            'stop_market', # CCXTの標準 Stop Market
            close_side,
            abs(contracts),
            price=None,
            params={
                'stopPrice': stop_loss, # トリガー価格
                # MEXC特有のパラメータ
                'triggerPrice': stop_loss, # MEXCのストップ価格
                'stopPriceType': 'market', # 決済タイプ: 成行
                'positionId': trade_result.get('info', {}).get('position_id'), # ポジションIDがあれば
            }
        )
        logging.info(f"✅ SL注文 ({close_side} @ {format_price(stop_loss)}) を発注しました。")
        
    except Exception as e:
        logging.error(f"❌ SL注文発注失敗: {e}")
        sl_order_result = {'status': 'error', 'error_message': str(e)}

    # 2. Take Profit (利益確定) 注文
    tp_order_result = {'status': 'N/A'}
    try:
        # Take Profitも同様に 'Take Profit Market' 注文として発注
        tp_order_result = await EXCHANGE_CLIENT.create_order(
            ccxt_symbol,
            'take_profit_market', # CCXTの標準 Take Profit Market
            close_side,
            abs(contracts),
            price=None,
            params={
                'stopPrice': take_profit, # トリガー価格
                # MEXC特有のパラメータ
                'triggerPrice': take_profit, # MEXCのストップ価格
                'stopPriceType': 'market', # 決済タイプ: 成行
                'positionId': trade_result.get('info', {}).get('position_id'), # ポジションIDがあれば
            }
        )
        logging.info(f"✅ TP注文 ({close_side} @ {format_price(take_profit)}) を発注しました。")
        
    except Exception as e:
        logging.error(f"❌ TP注文発注失敗: {e}")
        tp_order_result = {'status': 'error', 'error_message': str(e)}
        
    # SL/TPのいずれかが成功すればOKとする
    if sl_order_result.get('status') == 'ok' or tp_order_result.get('status') == 'ok':
        return {'status': 'ok', 'message': 'SL/TP注文を正常に設定しました。'}
    elif sl_order_result.get('status') == 'error' and tp_order_result.get('status') == 'error':
         return {'status': 'error', 'error_message': f"SL/TP両方の発注に失敗しました。SL: {sl_order_result.get('error_message')}, TP: {tp_order_result.get('error_message')}"}
    else:
        # 片方だけ成功した場合など
        return {'status': 'warning', 'message': 'SLまたはTPの一方、あるいは両方の発注に成功しました。'}


async def close_position(position_data: Dict, exit_type: str) -> Dict:
    """
    ポジションを成行でクローズする。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        logging.warning("⚠️ TEST_MODEが有効です。ポジションクローズは実行されません。")
        return {
            'status': 'ok', 
            'pnl_usdt': random.uniform(-1.0, 1.0) * 0.5 * FIXED_NOTIONAL_USDT * LEVERAGE, # 適当なPNL
            'pnl_rate': random.uniform(-0.1, 0.1),
            'exit_price': position_data['entry_price'] * (1 + random.uniform(-0.01, 0.01)),
            'filled_amount': position_data['contracts'],
            'entry_price': position_data['entry_price'],
            'message': f"TEST_MODE: ポジションクローズシミュレーション成功 ({exit_type})",
            'exit_type': exit_type,
        }

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    symbol = position_data['symbol']
    side = position_data['side']
    contracts = position_data['contracts']
    
    ccxt_symbol = symbol 
    if EXCHANGE_CLIENT.id == 'mexc':
        base = symbol.split('/')[0]
        quote = symbol.split('/')[1]
        ccxt_symbol = f"{base}/{quote}:{quote}"

    # ポジションのクローズ注文 (成行)
    close_side = 'sell' if side == 'long' else 'buy'
    
    try:
        # contractsは正負の値を持つが、注文量としては絶対値
        close_order = await EXCHANGE_CLIENT.create_order(
            ccxt_symbol,
            'market',
            close_side,
            abs(contracts),
            params={
                # MEXCの場合、ポジションのクローズに必要なパラメータ
                'openType': 2, # Cross Margin (ポジションオープン時の設定に合わせる)
                'positionType': 1 if side == 'long' else 2, # Long/Short (ポジションオープン時の設定に合わせる)
                # 全ポジションをクローズするためのフラグ (取引所による)
                'closePosition': True 
            }
        )
        
        # 決済の成功を確認
        if close_order.get('status') in ['closed', 'ok'] or close_order.get('info', {}).get('status') == 'FILLED':
            
            filled_amount = close_order.get('filled', abs(contracts))
            exit_price = close_order.get('average', 0.0)
            
            # PNLの取得 (フェッチが確実ではないため、ここでは一旦成功としてPNL計算は省略)
            # PNLの正確な計算は、取引所からの応答やfetch_positionsで確認すべきだが、ここでは簡略化。
            
            # PNLの推定（非常にラフな推定）
            # PNL(USD) = Contracts * (Exit_Price - Entry_Price) * (1 if long else -1)
            pnl_raw = abs(contracts) * (exit_price - position_data['entry_price'])
            if side == 'short':
                pnl_raw *= -1
                
            # PNL Rate (ROI)
            initial_margin_usdt = position_data['filled_usdt'] / LEVERAGE
            pnl_rate = pnl_raw / initial_margin_usdt if initial_margin_usdt > 0 else 0.0
            
            logging.info(f"✅ ポジションクローズ成功 ({exit_type}): {symbol} @ {format_price(exit_price)}, PNL: {pnl_raw:.2f} USDT")
            
            return {
                'status': 'ok',
                'pnl_usdt': pnl_raw,
                'pnl_rate': pnl_rate,
                'exit_price': exit_price,
                'filled_amount': filled_amount,
                'entry_price': position_data['entry_price'],
                'message': f"ポジションを正常にクローズしました。({exit_type})",
                'exit_type': exit_type,
            }

        else:
            error_msg = f"ポジションクローズ失敗: Status={close_order.get('status', 'N/A')}, Info={close_order.get('info')}"
            logging.error(f"❌ ポジションクローズ失敗: {error_msg}")
            return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}


    except Exception as e:
        error_msg = f"予期せぬポジションクローズエラー: {e}"
        logging.error(f"❌ ポジションクローズ失敗: {error_msg}", exc_info=True)
        return {'status': 'error', 'error_message': error_msg, 'exit_type': exit_type}


# ====================================================================================
# SCHEDULERS & MAIN LOOP
# ====================================================================================

async def process_entry_signal(signal_data: Dict, current_threshold: float) -> Optional[Dict]:
    """
    取引シグナルを処理し、取引の実行、ポジションの保存を行う。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS
    
    symbol = signal_data['symbol']
    side = signal_data['side']
    score = signal_data['score']
    
    now = time.time()
    
    # 冷却期間チェック
    last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
    if now - last_trade_time < TRADE_SIGNAL_COOLDOWN:
        logging.warning(f"⚠️ {symbol} - 冷却期間中のため取引をスキップしました。 ({now - last_trade_time:.0f}s / {TRADE_SIGNAL_COOLDOWN}s)")
        return None

    # ポジション重複チェック
    if any(p['symbol'] == symbol and p['side'] == side for p in OPEN_POSITIONS):
        logging.warning(f"⚠️ {symbol} - 既に同方向 ({side}) のポジションがあるため取引をスキップしました。")
        return None

    logging.info(f"🚀 取引実行: {symbol} ({side}) Score: {score:.2f}")

    # 1. 取引の実行
    trade_result = await execute_trade(signal_data)

    # 2. Telegram通知メッセージの整形と送信
    message = format_telegram_message(signal_data, "取引シグナル", current_threshold, trade_result)
    await send_telegram_notification(message)
    
    # 3. ログ記録
    log_signal(signal_data, "エントリーシグナル", trade_result)

    if trade_result.get('status') == 'ok':
        
        # 4. 成功した場合、SL/TPの設定を行う
        try:
            sl_tp_result = await manage_sl_tp(
                symbol,
                side,
                trade_result['filled_amount'],
                trade_result['filled_price'],
                signal_data['stop_loss'],
                signal_data['take_profit'],
                trade_result
            )
            
            # SL/TP結果をログに含める
            trade_result['sl_tp_status'] = sl_tp_result.get('status', 'N/A')
            trade_result['sl_tp_message'] = sl_tp_result.get('message', 'N/A')

        except Exception as e:
            logging.error(f"❌ SL/TP設定中にエラーが発生しました: {e}", exc_info=True)
            trade_result['sl_tp_status'] = 'error'
            trade_result['sl_tp_message'] = str(e)


        # 5. ポジション情報をグローバルリストに追加
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': trade_result['filled_amount'],
            'entry_price': trade_result['filled_price'],
            'filled_usdt': trade_result['filled_usdt'],
            'liquidation_price': calculate_liquidation_price(trade_result['filled_price'], LEVERAGE, side),
            'timestamp': int(now * 1000),
            'stop_loss': signal_data['stop_loss'],
            'take_profit': signal_data['take_profit'],
            'order_id': trade_result.get('order_id'),
            'metadata': trade_result # 取引結果全体をメタデータとして保持
        }
        OPEN_POSITIONS.append(new_position)
        
        # 冷却期間の時間を更新
        LAST_SIGNAL_TIME[symbol] = now
        
        logging.info(f"✅ ポジションをオープンしました: {symbol} ({side})")
        return new_position
        
    else:
        logging.error(f"❌ 取引実行失敗: {trade_result.get('error_message', '詳細不明')}")
        return None


async def monitor_position(position: Dict) -> Optional[Dict]:
    """
    オープンポジションを監視し、SL/TP/清算価格に達していないかチェックする。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    liquidation_price = position['liquidation_price']
    entry_price = position['entry_price']
    contracts = position['contracts']
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション監視失敗: CCXTクライアントが準備できていません。")
        return None

    # 最新価格の取得
    try:
        # CCXTのシンボルに変換 (例: BTC/USDT -> BTC/USDT:USDT)
        ccxt_symbol = symbol 
        if EXCHANGE_CLIENT.id == 'mexc':
            base = symbol.split('/')[0]
            quote = symbol.split('/')[1]
            ccxt_symbol = f"{base}/{quote}:{quote}"
            
        ticker = await EXCHANGE_CLIENT.fetch_ticker(ccxt_symbol)
        current_price = ticker['last']
        
    except Exception as e:
        logging.error(f"❌ {symbol} の現在価格取得失敗: {e}")
        return None
        
    exit_type = None
    
    if side == 'long':
        if current_price <= stop_loss:
            exit_type = "SL (ストップロス)"
        elif current_price >= take_profit:
            exit_type = "TP (テイクプロフィット)"
        elif current_price <= liquidation_price:
             exit_type = "Liquidation (清算価格)"
    elif side == 'short':
        if current_price >= stop_loss:
            exit_type = "SL (ストップロス)"
        elif current_price <= take_profit:
            exit_type = "TP (テイクプロフィット)"
        elif current_price >= liquidation_price:
             exit_type = "Liquidation (清算価格)"

    if exit_type:
        logging.warning(f"🔴 ポジションクローズトリガー: {symbol} ({side}) - {exit_type} @ {format_price(current_price)}")
        
        # 1. ポジションをクローズ
        close_result = await close_position(position, exit_type)
        
        # 2. Telegram通知
        # 決済価格/PNLを通知メッセージに含めるため、close_resultをtrade_resultとして渡す
        close_result['entry_price'] = entry_price # 元のエントリー価格を補完
        close_result['contracts'] = contracts # 元の契約数を補完
        
        # 通知メッセージ生成にはシグナル情報も必要だが、ここでは最小限のダミーシグナルを作成
        notification_signal = {
            'symbol': symbol,
            'side': side,
            'score': 1.0, # 決済なのでスコアはダミー
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': liquidation_price,
            'rr_ratio': 0.0, # ダミー
        }
        
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 現在の閾値を取得
        message = format_telegram_message(notification_signal, "ポジション決済", current_threshold, close_result, exit_type)
        await send_telegram_notification(message)

        # 3. ログ記録
        log_signal(notification_signal, "ポジション決済", close_result)
        
        return close_result # クローズが成功/失敗に関わらず、トリガーされた結果を返す

    return None


async def position_monitor_scheduler():
    """オープンポジションのクローズ条件を定期的にチェックするスケジューラ"""
    global OPEN_POSITIONS
    
    logging.info("⏳ ポジション監視スケジューラを開始しました。")
    
    while True:
        try:
            if not IS_CLIENT_READY:
                logging.info("ℹ️ クライアントが未初期化のため、ポジション監視をスキップします。")
                await asyncio.sleep(MONITOR_INTERVAL)
                continue
                
            # fetch_account_status で OPEN_POSITIONS が更新される (主にAPIでポジションが消えたかを確認)
            await fetch_account_status()
            
            positions_to_remove = []
            
            for position in OPEN_POSITIONS:
                # 監視ロジックを実行
                close_result = await monitor_position(position)
                
                if close_result:
                    # ポジションクローズがトリガーされた場合
                    positions_to_remove.append(position)
                    
            # クローズが成功/失敗に関わらず、グローバルリストから削除 (APIで確認済みのポジションのみ残るべきだが、二重クローズ防止のため)
            if positions_to_remove:
                OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
                logging.info(f"🗑️ ポジションを {len(positions_to_remove)} 件クローズし、リストから削除しました。残り: {len(OPEN_POSITIONS)} 件")

        except Exception as e:
            logging.error(f"❌ ポジション監視ループで致命的なエラーが発生しました: {e}", exc_info=True)
            
        await asyncio.sleep(MONITOR_INTERVAL)


async def analyze_and_generate_signals(monitoring_symbols: List[str]) -> List[Dict]:
    """
    全監視銘柄の分析とシグナル生成を行う。
    """
    global GLOBAL_MACRO_CONTEXT
    signals: List[Dict] = []
    
    # 0. マクロコンテキストの更新
    await fetch_fgi_context() 
    
    # 1. 各銘柄/時間軸で分析
    for symbol in monitoring_symbols:
        for tf in TARGET_TIMEFRAMES:
            
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
            df = await fetch_ohlcv(symbol, tf, limit)
            
            if df is None or df.empty or len(df) < limit:
                continue

            # 2. 指標の適用とNaNの削除
            df_ta = apply_technical_indicators(df)
            if df_ta is None or df_ta.empty:
                continue

            # 3. ロング/ショートの両方向でシグナルを評価
            for side in ['long', 'short']:
                
                # 4. スコア計算
                score, tech_data = calculate_signal_score(symbol, df_ta, side, GLOBAL_MACRO_CONTEXT)

                if score > BASE_SCORE: 
                    # ベーススコアを超えたシグナルのみを記録
                    
                    # 5. リスク/ターゲットの計算 (ここでは固定ロットに基づくリスク額も計算)
                    entry_price, stop_loss, take_profit, risk_usdt, rr_ratio = calculate_risk_and_targets(
                        df_ta, 
                        side, 
                        risk_tolerance_usdt=0.0 # 固定ロットのためここでは使用しない
                    )
                    
                    liquidation_price = calculate_liquidation_price(entry_price, LEVERAGE, side)

                    signals.append({
                        'symbol': symbol,
                        'timeframe': tf,
                        'side': side,
                        'score': score,
                        'entry_price': entry_price,
                        'stop_loss': stop_loss,
                        'take_profit': take_profit,
                        'liquidation_price': liquidation_price,
                        'risk_usdt': risk_usdt,
                        'rr_ratio': rr_ratio,
                        'tech_data': tech_data,
                        'timestamp': df_ta.index[-1].value // 10**6 # 最新バーの確定時刻 (ms)
                    })
                    
    
    logging.info(f"📊 分析完了: 生成されたシグナル総数: {len(signals)} 件。")
    return signals


async def main_bot_scheduler():
    """メインのボットスケジューラ。データ取得、分析、取引実行を行う。"""
    
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, LAST_WEBSHARE_UPLOAD_TIME

    logging.info("⏳ メインボットスケジューラを開始しました。")
    
    # 1. CCXTクライアントの初期化を試行
    if not IS_CLIENT_READY:
        await initialize_exchange_client()
        
    # 2. 初回起動時の状態チェック
    account_status = await fetch_account_status()
    
    # 3. 監視シンボルの初期リストを取得
    monitoring_symbols = await fetch_available_symbols()

    # 4. 初回起動メッセージを送信
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(monitoring_symbols), current_threshold)
    await send_telegram_notification(startup_message)
    
    # 5. メインループの開始
    while True:
        try:
            current_time = time.time()
            
            if not IS_CLIENT_READY:
                logging.critical("❌ クライアントが使用できません。再初期化を試みます。")
                await initialize_exchange_client()
                if not IS_CLIENT_READY:
                    # 再初期化失敗の場合、長時間待機して再試行
                    await asyncio.sleep(LOOP_INTERVAL * 5)
                    continue

            # ----------------------------------------------------
            # A. 定期的なデータ更新とロジック実行
            # ----------------------------------------------------
            
            # 1. 監視シンボルリストの更新 (1時間に1回)
            if current_time - LAST_SUCCESS_TIME > WEBSHARE_UPLOAD_INTERVAL or not IS_FIRST_MAIN_LOOP_COMPLETED:
                monitoring_symbols = await fetch_available_symbols()
            
            # 2. アカウントステータスの更新
            # fetch_account_status は position_monitor_scheduler でも呼ばれるため、ここではオプション
            # account_status = await fetch_account_status() 

            # 3. 分析とシグナル生成
            signals = await analyze_and_generate_signals(monitoring_symbols)
            LAST_ANALYSIS_SIGNALS = signals
            
            # 4. 動的な取引閾値の取得
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 5. シグナルのフィルタリング
            tradable_signals = [s for s in signals if s['score'] >= current_threshold]
            
            # 6. 最高スコアのシグナルに絞り込み
            if tradable_signals:
                # スコアとRRRの積でソート (RRRがない場合はスコアのみ)
                tradable_signals.sort(key=lambda x: x['score'] * (x.get('rr_ratio', 1.0)), reverse=True)
                top_signals = tradable_signals[:TOP_SIGNAL_COUNT]
            else:
                top_signals = []
            
            logging.info(f"📈 フィルタリング後、取引可能なシグナル数: {len(top_signals)} (閾値: {current_threshold*100:.2f}点)")

            # 7. 取引の実行
            for signal in top_signals:
                await process_entry_signal(signal, current_threshold)

            # ----------------------------------------------------
            # B. 定期的な通知とWebShare更新
            # ----------------------------------------------------
            
            # 1. 閾値未満の最高スコア通知
            await notify_highest_analysis_score()
            
            # 2. WebShareデータの送信 (1時間に1回)
            if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions': OPEN_POSITIONS,
                    'last_signals': top_signals,
                    'all_analysis_signals': LAST_ANALYSIS_SIGNALS,
                    'macro_context': GLOBAL_MACRO_CONTEXT,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time
            
            # 8. ループ完了
            LAST_SUCCESS_TIME = current_time
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except Exception as e:
            logging.error(f"❌ メイン分析ループで致命的なエラーが発生しました: {e}", exc_info=True)
            # 致命的なエラーが発生した場合、安全のため少し長めに待機
            await asyncio.sleep(LOOP_INTERVAL * 5)
            continue
            
        await asyncio.sleep(LOOP_INTERVAL)


# ====================================================================================
# WEB SERVER & ENTRY POINT (FastAPI/Uvicorn)
# ====================================================================================

# FastAPIアプリケーションの初期化
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
    logging.info(f"Uvicornサーバーを開始します。ポート: {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
