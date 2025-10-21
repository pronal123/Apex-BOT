# ====================================================================================
#Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.70, 0.65, 0.60) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
# 4. 【致命的修正】 calculate_technical_indicators 内の BBANDS (BBL/BBM/BBH) 取得ロジックを動的キー検索に修正し、KeyError: 'BBL' を回避。
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
import ftplib 
import uuid 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", "FTM/USDT", "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", "EOS/USDT", "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "MKR/USDT", "THETA/USDT", "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)を40に引き上げ
LOOP_INTERVAL = 60 * 10             # メインループの実行間隔 (秒) - 10分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 💡 WEBSHARE設定 (FTP/WebDAVなど、外部ログストレージを想定)
WEBSHARE_HOST = os.getenv("WEBSHARE_HOST")
WEBSHARE_PORT = int(os.getenv("WEBSHARE_PORT", "21")) # デフォルトはFTPポート
WEBSHARE_USER = os.getenv("WEBSHARE_USER")
WEBSHARE_PASS = os.getenv("WEBSHARE_PASS")

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {} # マクロコンテキストを保持するための変数
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン (通常時の値 2-3銘柄/日を想定)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['15m', '1h', '4h']
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.07       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - Patch 36確定)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: 頻度目標達成のため閾値を引き下げ (この値で確定)
SIGNAL_THRESHOLD_SLUMP = 0.70       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.65      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.60      # 活発時の閾値 (3+銘柄/日を想定)

RSI_DIVERGENCE_BONUS = 0.10         # RSIダイバージェンス時のボーナス (未使用だが定数として残す)
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # ボラティリティ過熱時のペナルティ閾値
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致時のボーナス

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    # スコア算出ロジックから取得できる定数値 (通知表示に利用)
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア (全ての分析の出発点)
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    # 価格構造/ピボット支持ボーナス
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    # 流動性ボーナス
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    # FGIマクロ要因
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    # 為替マクロ (常に0.0を表示)
    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    # ボラティリティペナルティ (負の値のみ表示)
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) 

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = f"低迷/リスクオフ (Threshold: {SIGNAL_THRESHOLD_SLUMP*100:.0f}点)"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = f"活発/リスクオン (Threshold: {SIGNAL_THRESHOLD_ACTIVE*100:.0f}点)"
    else:
        market_condition_text = f"通常/中立 (Threshold: {SIGNAL_THRESHOLD_NORMAL*100:.0f}点)"
    

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報 (Rank 1のみに焦点を当てる)
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
        # スコア詳細ブレークダウンの生成
        breakdown_details = get_score_breakdown(top_signal)
        
        score_color = ""
        if score < current_threshold:
             score_color = "⚠️" 
        if score < BASE_SCORE: 
             score_color = "🔴"
             
        rr_display = f"1:{rr_ratio:.1f}" if rr_ratio >= 1.0 else f"1:{rr_ratio:.1f} ❌"
        
        signal_section += (
            f"  🥇 <b>{symbol}</b> ({timeframe}) - **最高スコア** {score_color}\n"
            f"     - **総合スコア**: <code>{score * 100:.2f} / 100</code> (推定勝率: {get_estimated_win_rate(score)})\n"
            f"     - **リスクリワード比率 (RRR)**: <code>{rr_display}</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

        # 警告メッセージの追加
        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>" 
    )

    return header + macro_section + signal_section + footer

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # マクロ情報
    fgi_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")

    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    # 自動売買ステータス
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" # BASEに変更
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. 残高/ポジション情報
    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        # エラーメッセージを分かりやすく表示
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # 管理ポジションの表示
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_usdt(pos['stop_loss'])} / TP: {format_usdt(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

        # 既存の現物ポジション（CCXTから取得）は簡略化
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             balance_section += f"  - **未管理の現物**: <code>{len(open_ccxt_positions)}</code> 銘柄 (CCXT参照)\n"
        
    balance_section += f"\n"

    # 2. 市場状況 (スコア付け)
    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_value}</code> ({fgi_sentiment})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約10分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # SL/TP/RRRはシグナルまたは取引結果から取得
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0))
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0))
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0))
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) 

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        # エントリー通知
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) # 動的ロット
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(lot_size)} USDT)"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            filled_amount = trade_result.get('filled_amount', 'N/A')
            filled_usdt = trade_result.get('filled_usdt', 'N/A')
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: <code>{format_usdt(lot_size)}</code> USDT (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        # 決済通知
        trade_status_line = f"🔴 **ポジション決済**: {exit_type} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 'N/A')
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_usdt(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_usdt(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"📌 **ポジション管理パラメータ**\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # 取引結果セクションをシグナル詳細の前に追加
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # エントリー時のみスコアブレークダウンを表示
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>")
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
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

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    特にboolやNumPyのスカラ型を文字列/Pythonネイティブ型に変換する。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        # すべてのブーリアン型を文字列に変換し、シリアライズエラーを回避
        return str(obj) 
    elif isinstance(obj, np.generic):
        # numpy.float64, numpy.int64 などのNumPyスカラをPythonネイティブ型に変換
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする"""
    try:
        # ロギングされるデータの基本構造を定義
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None), # 決済時にはtrade_resultがdata内に含まれる
            'full_data': data,
        }
        
        # JSONシリアライズエラーを回避するために辞書をクリーニング
        cleaned_log_entry = _to_json_compatible(log_entry)

        # ファイル名にスペースが入らないように修正
        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


def _sync_ftp_upload(local_file: str, remote_file: str):
    """
    同期的にFTPアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    """
    if not WEBSHARE_HOST or not WEBSHARE_USER or not WEBSHARE_PASS:
        logging.error("❌ WEBSHARE設定 (HOST/USER/PASS) が不足しています。")
        return False

    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True # ファイルがないのはエラーではない

    try:
        # FTP接続とログイン
        ftp = ftplib.FTP()
        # 💡 タイムアウトを30秒に延長 (FTPタイムアウト対策)
        ftp.connect(WEBSHARE_HOST, WEBSHARE_PORT, timeout=30) 
        ftp.login(WEBSHARE_USER, WEBSHARE_PASS)
        
        # ファイルのアップロード (バイナリモード)
        # リモートパスは /<filename> の形式を想定
        ftp.storbinary(f'STOR {remote_file}', open(local_file, 'rb'))

        ftp.quit()
        return True
        
    except ftplib.all_errors as e:
        logging.error(f"❌ FTPアップロードエラー ({WEBSHARE_HOST}): {e}")
        return False
    except Exception as e:
        logging.error(f"❌ ログアップロードの予期せぬエラー: {e}")
        return False

async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/FTP) にアップロードする"""
    if not WEBSHARE_HOST:
        logging.info("ℹ️ WEBSHARE HOSTが設定されていません。ログアップロードをスキップします。")
        return
        
    log_files = [
        "apex_bot_trade_signal_log.jsonl",
        "apex_bot_hourly_analysis_log.jsonl",
        "apex_bot_trade_exit_log.jsonl",
    ]
    
    now_jst = datetime.now(JST)
    upload_timestamp = now_jst.strftime("%Y%m%d_%H%M%S")
    
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します...")

    tasks = []
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            
            # 同期FTP処理を別スレッドで実行
            tasks.append(
                asyncio.to_thread(_sync_ftp_upload, log_file, remote_filename)
            )

    if not tasks:
        logging.info("ℹ️ アップロード対象のログファイルがありませんでした。")
        return

    # 全てのタスクを並行実行
    results = await asyncio.gather(*tasks)
    
    if all(results):
        logging.info(f"✅ すべてのログファイル ({len(tasks)} 件) を WEBSHARE にアップロードしました。")
    else:
        logging.error("❌ 一部またはすべてのログファイルの WEBSHARE へのアップロードに失敗しました。")

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        # MEXCクライアント
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        # CCXTのオプション設定
        options = {
            'defaultType': 'spot', # 現物取引 (Spot) を想定
        }

        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化しました。")
        return True

    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        EXCHANGE_CLIENT = None
        return False

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df

    except Exception as e:
        return None

async def fetch_ohlcv_with_fallback(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定した足数を確実に取得するためにフォールバック処理を行う"""
    df = await fetch_ohlcv_safe(symbol, timeframe, limit)
    
    # データが少ない場合は、より多くの足数を要求してリトライ
    if df is None or len(df) < limit:
        logging.debug(f"ℹ️ {symbol} ({timeframe}): 必要足数 ({limit}) 未満。リトライ中...")
        df_long = await fetch_ohlcv_safe(symbol, timeframe, limit=limit + 100)
        
        if df_long is not None and len(df_long) >= limit:
            return df_long
        elif df_long is not None and len(df_long) > 0:
            return df_long

    return df

async def update_symbols_by_volume() -> None:
    """出来高に基づいて監視対象銘柄リストを更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 銘柄リスト更新をスキップしました。")
        return

    try:
        # すべてのUSDTペアの現物銘柄を取得
        markets = await EXCHANGE_CLIENT.load_markets()
        usdt_spot_symbols = [
            s for s, m in markets.items() 
            if m['active'] and m['spot'] and (m['quote'] == 'USDT' or s.endswith('/USDT'))
        ]

        if not usdt_spot_symbols:
            logging.warning("⚠️ USDT現物銘柄が見つかりませんでした。デフォルトリストを使用します。")
            CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_SYMBOLS))
            logging.info(f"✅ 銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
            return

        # 24時間出来高情報を取得 (ccxtのfetch_tickersでquoteVolumeを使用)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(usdt_spot_symbols)
        
        volume_data = []
        for symbol in usdt_spot_symbols:
            if symbol in tickers and 'quoteVolume' in tickers[symbol]:
                 volume_data.append({
                    'symbol': symbol,
                    'volume': tickers[symbol]['quoteVolume']
                })
                
        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [d['symbol'] for d in volume_data if d['volume'] is not None][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルと出来高トップをマージし、重複を排除
        combined_symbols = top_symbols + [s for s in DEFAULT_SYMBOLS if s not in top_symbols]
        
        # 最終リストの更新 (市場に存在する銘柄のみ)
        CURRENT_MONITOR_SYMBOLS = [s for s in combined_symbols if s in markets]

        # 銘柄数が30未満の場合は警告
        if len(CURRENT_MONITOR_SYMBOLS) < 30:
            logging.warning(f"⚠️ 監視銘柄数が30未満 ({len(CURRENT_MONITOR_SYMBOLS)}) です。静的リストの追加をご検討ください。")

        logging.info(f"✅ 現物銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
        logging.debug(f"現在の監視銘柄リスト: {CURRENT_MONITOR_SYMBOLS}")

    except Exception as e:
        logging.error(f"❌ 銘柄リスト更新エラー: {e}")
        # エラー時は既存のリストを維持

def fetch_fgi_sync() -> int:
    """Fear & Greed Index (FGI) を取得する (同期処理)"""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data and 'data' in data and len(data['data']) > 0:
            return int(data['data'][0]['value'])
        return 50 # 取得失敗時は中立
    except Exception as e:
        logging.error(f"❌ FGI APIエラー: {e}")
        return 50

async def get_crypto_macro_context() -> Dict:
    """市場全体のマクロコンテキストを取得する (FGI リアルデータ取得)"""
    fgi_value = 50
    fgi_proxy = 0.0
    forex_trend = 'NEUTRAL'
    forex_bonus = 0.0

    # 1. 恐怖・貪欲指数 (FGI) を取得
    try:
        fgi_value = await asyncio.to_thread(fetch_fgi_sync)
        # FGIを正規化し、-1から1の範囲でマクロ影響度を計算
        fgi_normalized = (fgi_value - 50) / 20.0
        # 最大ボーナス/ペナルティに制限
        fgi_proxy = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_normalized * FGI_PROXY_BONUS_MAX))
    except Exception as e:
        logging.error(f"❌ FGI取得エラー: {e}")

    # 2. 為替マクロデータは削除

    # 3. マクロコンテキストの確定
    return {
        'fgi_raw_value': fgi_value,
        'fgi_proxy': fgi_proxy,
        'forex_trend': forex_trend,
        'forex_bonus': forex_bonus,
    }

async def fetch_account_status() -> Dict:
    """口座残高と保有ポジション（10 USDT以上）を取得する"""
    global EXCHANGE_CLIENT
    status = {
        'total_usdt_balance': 0.0,
        'open_positions': [], # CCXTから取得した現物
        'error': None
    }
    
    if not EXCHANGE_CLIENT:
        status['error'] = "Exchange client not initialized."
        return status

    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        status['total_usdt_balance'] = balance['total'].get('USDT', 0.0)

        # 2. ポジションの取得 (USDT以外の保有資産) と価格取得
        for currency, info in balance['total'].items():
            if not isinstance(currency, str) or currency == 'USDT' or info == 0.0 or info < 0.000001:
                continue
                
            symbol = f"{currency}/USDT"
            if symbol in EXCHANGE_CLIENT.markets:
                try:
                    # 堅牢性を優先し、ティッカーを個別に取得
                    ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                    market_price = ticker['last']
                    usdt_value = info * market_price

                    # 10 USDT以上の価値があるもののみをポジションとして報告
                    if usdt_value >= 10:
                        status['open_positions'].append({
                            'symbol': symbol,
                            'base_amount': info,
                            'market_price': market_price,
                            'usdt_value': usdt_value,
                        })
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} の価格取得またはCCXT処理エラー: {e}")


    except Exception as e:
        status['error'] = f"Account status fetch error: {type(e).__name__}"
        logging.error(f"❌ 口座ステータス取得エラー: {e}")
        
    return status

async def execute_trade(symbol: str, trade_size_usdt: float) -> Dict:
    """
    現物 (Spot) の成行買いを実行する。
    
    Args:
        symbol: 取引シンボル (例: 'BTC/USDT')
        trade_size_usdt: 投入するUSDT金額 (目標)

    Returns:
        取引結果を格納した辞書
    """
    global EXCHANGE_CLIENT
    result = {
        'status': 'error',
        'error_message': '取引所クライアントが未初期化です。',
        'filled_amount': 0.0,
        'filled_usdt': 0.0,
        'entry_price': 0.0,
    }

    if not EXCHANGE_CLIENT:
        return result
        
    if TEST_MODE:
        result['error_message'] = "TEST_MODEが有効です。取引は実行されません。"
        return result
        
    try:
        # 1. 銘柄情報の確認 (精度、最小取引量)
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market or not market['active']:
            result['error_message'] = f"銘柄 {symbol} は取引所で見つからないか、非アクティブです。"
            return result
        
        # 2. 現在価格を取得し、購入するベース通貨の数量を計算
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        current_price = ticker['last']
        
        # 3. 数量の精度調整
        # 精度設定 (amountPrecision) がない場合はデフォルトで8桁を使用
        amount_precision = market.get('precision', {}).get('amount', 8)
        
        # ベース通貨での数量
        base_amount = trade_size_usdt / current_price
        
        # 数量を取引所の精度に合わせて丸める
        # ccxt.decimal_to_precision を使用して、取引所固有の丸め処理を適用
        amount_str = EXCHANGE_CLIENT.decimal_to_precision(
            base_amount, 
            ccxt.ROUND, 
            amount_precision,
            ccxt.DECIMAL_PLACES
        )
        amount = float(amount_str)

        # 4. 最小取引量の確認 (現物取引での最小数量 or 最小USDT価格)
        min_notional = market.get('limits', {}).get('cost', {}).get('min', 10) # 最小USDT (多くの取引所は10USDT)
        
        if amount * current_price < min_notional:
            result['error_message'] = f"計算されたロットサイズ ({amount*current_price:.2f} USDT) が最小取引額 ({min_notional} USDT) を下回っています。"
            return result
        
        # 5. 注文の実行 (成行買い - 'market' order)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='buy',
            amount=amount, 
            params={}
        )
        
        # 6. 約定結果の確認
        if order and order['status'] == 'closed':
            # 約定価格、約定数量、手数料を計算
            filled_amount = order['filled']
            filled_cost = order['cost'] # USDTでの支払総額
            average_price = order['price'] if order['price'] else (filled_cost / filled_amount if filled_amount else current_price)
            
            result['status'] = 'ok'
            result['filled_amount'] = filled_amount
            result['filled_usdt'] = filled_cost
            result['entry_price'] = average_price
            
            logging.info(f"✅ 取引成功: {symbol}, 数量: {filled_amount:.4f}, 平均価格: {average_price:.4f}, 投入額: {filled_cost:.2f} USDT")
            return result
        else:
            # 部分約定または失敗
            result['error_message'] = f"注文は送信されましたが、完全に約定しませんでした。ステータス: {order.get('status', 'N/A')}"
            logging.warning(f"⚠️ 取引警告: {result['error_message']}")
            return result


    except ccxt.DDoSProtection as e:
        result['error_message'] = "DDoS保護: レート制限に達しました。"
        logging.error(f"❌ 取引エラー: {result['error_message']}, {e}")
    except ccxt.ExchangeError as e:
        result['error_message'] = f"取引所エラー: {str(e)}"
        logging.error(f"❌ 取引エラー: {result['error_message']}, {e}")
    except Exception as e:
        result['error_message'] = f"予期せぬエラー: {type(e).__name__} - {str(e)}"
        logging.error(f"❌ 取引エラー: {result['error_message']}, {e}")
        
    return result


async def execute_exit_trade(position: Dict) -> Dict:
    """
    ポジションを現物 (Spot) の成行売りで決済する。
    
    Args:
        position: ポジション情報 (symbol, base_amountを持つ)

    Returns:
        決済結果を格納した辞書
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    base_amount_to_sell = position['filled_amount']

    result = {
        'status': 'error',
        'error_message': '取引所クライアントが未初期化です。',
        'exit_price': 0.0,
        'filled_amount': 0.0,
        'filled_usdt': 0.0,
    }

    if not EXCHANGE_CLIENT:
        return result
        
    if TEST_MODE:
        result['error_message'] = "TEST_MODEが有効です。取引は実行されません。"
        return result
        
    try:
        # 1. 銘柄情報の確認 (精度)
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market or not market['active']:
            result['error_message'] = f"銘柄 {symbol} は取引所で見つからないか、非アクティブです。"
            return result
        
        # 2. 数量の精度調整
        amount_precision = market.get('precision', {}).get('amount', 8)
        
        # 数量を取引所の精度に合わせて丸める (売り注文なので、保有数量をそのまま使用)
        amount_str = EXCHANGE_CLIENT.decimal_to_precision(
            base_amount_to_sell, 
            ccxt.ROUND_DOWN, # 売り注文なので、切り捨てで丸め、残高不足を回避
            amount_precision,
            ccxt.DECIMAL_PLACES
        )
        amount = float(amount_str)

        # 3. 注文の実行 (成行売り - 'market' order, 'sell')
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side='sell',
            amount=amount, 
            params={}
        )
        
        # 4. 約定結果の確認
        if order and order['status'] == 'closed':
            # 約定価格、約定数量、USDTでの受取総額を計算
            filled_amount = order['filled']
            filled_cost = order['cost'] # USDTでの受取総額
            average_price = order['price'] if order['price'] else (filled_cost / filled_amount if filled_amount else 0.0)
            
            result['status'] = 'ok'
            result['filled_amount'] = filled_amount
            result['filled_usdt'] = filled_cost
            result['exit_price'] = average_price
            
            logging.info(f"✅ 決済成功: {symbol}, 数量: {filled_amount:.4f}, 平均価格: {average_price:.4f}, 受取額: {filled_cost:.2f} USDT")
            return result
        else:
            # 部分約定または失敗
            result['error_message'] = f"決済注文は送信されましたが、完全に約定しませんでした。ステータス: {order.get('status', 'N/A')}"
            logging.warning(f"⚠️ 決済警告: {result['error_message']}")
            return result


    except ccxt.DDoSProtection as e:
        result['error_message'] = "DDoS保護: レート制限に達しました。"
        logging.error(f"❌ 決済エラー: {result['error_message']}, {e}")
    except ccxt.ExchangeError as e:
        result['error_message'] = f"取引所エラー: {str(e)}"
        logging.error(f"❌ 決済エラー: {result['error_message']}, {e}")
    except Exception as e:
        result['error_message'] = f"予期せぬエラー: {type(e).__name__} - {str(e)}"
        logging.error(f"❌ 決済エラー: {result['error_message']}, {e}")
        
    return result

# ====================================================================================
# TECHNICAL ANALYSIS CORE
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたDataFrameにテクニカル指標を計算して追加する。
    """
    if df.empty:
        return df

    # 1. 移動平均線 (SMA)
    # トレンドフィルタ用 長期SMA
    df['SMA_LONG'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    # 短期SMA (BBANDの代替MAM)
    df['SMA_20'] = ta.sma(df['close'], length=20) 

    # 2. RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14)

    # 3. MACD (Moving Average Convergence Divergence)
    # pandas_taのMACDは通常 MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9 を返す
    macd_data = df.ta.macd(append=False)
    
    # MACDのカラム名を動的に探し、KeyErrorを回避
    macd_cols = [col for col in macd_data.columns if col.startswith('MACD_') and 'h' not in col and 's' not in col]
    hist_cols = [col for col in macd_data.columns if col.startswith('MACDh_')]
    signal_cols = [col for col in macd_data.columns if col.startswith('MACDs_')]
    
    try:
        if macd_cols:
            df['MACD'] = macd_data[macd_cols[0]]
        else:
            df['MACD'] = np.nan
        
        if hist_cols:
            df['MACD_Hist'] = macd_data[hist_cols[0]]
        else:
            df['MACD_Hist'] = np.nan
            
        if signal_cols:
            df['MACD_Signal'] = macd_data[signal_cols[0]]
        else:
            df['MACD_Signal'] = np.nan
            
    except Exception as e:
        df['MACD'] = np.nan
        df['MACD_Hist'] = np.nan
        df['MACD_Signal'] = np.nan
        logging.warning(f"⚠️ MACDのキーが見つかりませんでした: {e}")
        

    # 3. ボリンジャーバンド (BBAND)
    # パラメータ (Length=20, StdDev=2.0)
    # pandas_taのBBANDSは、BBL (Lower), BBP (Percent), BBS (Standard Deviation), BBH (Higher), BBM (Mid) を返す
    bb_data = df.ta.bbands(length=20, std=2.0, append=False)
    
    # 💡 【修正点】KeyError: 'BBL' および 'BBL_20_2.0' を回避するため、動的にカラム名を検索し、存在しない場合はNaNを割り当てる
    
    # bb_data.columnsから、各バンドのプレフィックスに一致するカラム名を探す
    # pandas_taのバージョンによって 'BBL' または 'BBL_20_2.0' の形式になるため、プレフィックス検索で対応
    bbl_cols = [col for col in bb_data.columns if col.startswith('BBL_') or col == 'BBL']
    bbm_cols = [col for col in bb_data.columns if col.startswith('BBM_') or col == 'BBM']
    bbh_cols = [col for col in bb_data.columns if col.startswith('BBH_') or col == 'BBH']
    
    # BBL (Lower Band) の取得
    if bbl_cols:
        # プレフィックスに一致した最初に見つかったカラムを使用
        df['BBL'] = bb_data[bbl_cols[0]]
    else:
        df['BBL'] = np.nan
        logging.warning("⚠️ BBANDS (BBL) のキーが見つかりませんでした。データフレームのインデックスを確認してください。")

    # BBM (Mid Band - SMA) の取得
    if bbm_cols:
        df['BBM'] = bb_data[bbm_cols[0]] 
    else:
        df['BBM'] = np.nan
        logging.warning("⚠️ BBANDS (BBM) のキーが見つかりませんでした。")
        
    # BBH (Upper Band) の取得
    if bbh_cols:
        df['BBH'] = bb_data[bbh_cols[0]]
    else:
        df['BBH'] = np.nan
        logging.warning("⚠️ BBANDS (BBH) のキーが見つかりませんでした。")
    
    # ボリンジャーバンドの幅 (BBW) とパーセンテージ (%B) - EMAベース
    bbw_data = df.ta.bbands(length=20, std=2.0, append=False, mamode='ema', col_names=('BBLE', 'BBME', 'BBHE', 'BBWE', 'BBPE'))

    # ここもKeyError発生の可能性があるためtry/exceptで囲む
    try:
        df['BBW'] = bbw_data['BBWE']
        df['BBP'] = bbw_data['BBPE']
    except KeyError:
        # EMAベースのキーがない場合
        df['BBW'] = np.nan
        df['BBP'] = np.nan
        logging.warning("⚠️ BBANDS (EMA: BBW/BBP) のキーが見つかりませんでした。")

    # 4. ATR (Average True Range)
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)

    # 5. OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'], append=False)
    # OBVのSMA (トレンド確認用)
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)

    # 6. ピボットポイント (Pivot Points)
    # リアルタイムのピボットは難しいので、過去の期間（例：1日）のピボットポイントを計算
    # 1Dのデータがあれば使用するが、ここでは最も細かい15mの終値を使用して簡易計算
    pivot_data = df.ta.pivot_points(kind='fibonacci', append=False)
    
    # ピボットデータのカラム名を動的に探し、KeyErrorを回避
    r1_cols = [col for col in pivot_data.columns if 'R1' in col]
    s1_cols = [col for col in pivot_data.columns if 'S1' in col]

    try:
        if r1_cols and s1_cols:
             # 直近のR1とS1を取得
            df['PP_R1'] = pivot_data[r1_cols[-1]]
            df['PP_S1'] = pivot_data[s1_cols[-1]]
            df['PP_Pivot'] = pivot_data['PP_F'] # フィボナッチピボットのPを代入
        else:
            df['PP_R1'] = np.nan
            df['PP_S1'] = np.nan
            df['PP_Pivot'] = np.nan
            logging.warning("⚠️ ピボットポイントのキーが見つかりませんでした。")

    except Exception as e:
        df['PP_R1'] = np.nan
        df['PP_S1'] = np.nan
        df['PP_Pivot'] = np.nan
        logging.warning(f"⚠️ ピボットポイント計算エラー: {e}")


    # 7. スムージング
    # スムージングが必要なインジケータは計算済み

    # 最後の行のみを分析に使用するため、NaNをドロップしない
    return df

def apply_analysis_score(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """
    テクニカル指標とマクロコンテキストに基づいてロングシグナルのスコアを計算し、
    リスクリワード比率 (RRR) が1.0以上で、長期トレンド逆行ペナルティが0.15以下のシグナルのみを返す。
    """
    if df.empty or len(df) < 2:
        return None

    # 最新のデータを取得
    last = df.iloc[-1]
    
    # 終値がNaNの場合は分析をスキップ
    if pd.isna(last['close']):
        return None

    # ====================================================================
    # 1. SL/TPの設定 (長期トレンドの終値とATRに基づく動的なSL/TP)
    # ====================================================================
    
    current_price = last['close']
    
    # 1.1. SL (Stop Loss) の設定:
    # 直近のローソク足の安値と ATR の組み合わせを使用し、直近の構造的安値から離れすぎないように設定
    
    # ATRベースのSL距離 (例: 2 * ATR)
    atr_sl_distance = last['ATR'] * 2.5 
    
    # BB Lower BandをSLのベースラインとして使用
    structural_sl_price = last['BBL'] 
    
    # SL価格: BB Lower と (現在価格 - ATR距離) のうち、より厳格な方（上）を使用
    # BBLは現在価格より常に下にあるため、(現在価格 - ATR距離)より高い（安全な）BBLを優先
    stop_loss_price = max(structural_sl_price, current_price - atr_sl_distance)
    
    # SLが現在価格より上にある場合（データ異常、トレンド転換中など）は分析しない
    if stop_loss_price >= current_price:
        return None

    # 1.2. TP (Take Profit) の設定:
    # ATRに基づく距離 (例: 5 * ATR) を最低目標として、BB Upper BandまたはピボットR1を使用
    
    atr_tp_distance = atr_sl_distance * 2.0 # RRR 2.0を目標とする距離
    
    # TP価格候補: ATR距離、BB Upper Band、ピボットR1
    tp_candidate_1 = current_price + atr_tp_distance
    tp_candidate_2 = last['BBH']
    tp_candidate_3 = last['PP_R1']
    
    # TP価格: 3つの候補のうち、最も安全な方（下）を使用
    take_profit_price = min(tp_candidate_1, tp_candidate_2, tp_candidate_3)
    
    # TPが現在価格より下にある場合は分析しない
    if take_profit_price <= current_price:
        return None
        
    # 1.3. RRR (Risk Reward Ratio) の計算
    risk = current_price - stop_loss_price
    reward = take_profit_price - current_price
    
    if risk <= 0:
         return None
         
    rr_ratio = reward / risk

    # RRRが最低基準 (1.0) を満たさない場合はシグナルとしない
    if rr_ratio < 1.0:
        return None
        
    # ====================================================================
    # 2. スコアリングの実行 (基本60点 + 加点/減点)
    # ====================================================================

    score = BASE_SCORE # 60点からスタート
    tech_data = {} # スコア詳細記録用

    # 2.1. 長期トレンドフィルタ (SMA 200) - ペナルティ/回避ボーナス
    # SMA 200が計算できない場合もペナルティは適用しない（トレンド不明確と見なす）
    long_term_reversal_penalty_value = 0.0
    is_long_term_bullish = False
    
    if pd.notna(last['SMA_LONG']):
        if current_price < last['SMA_LONG']:
            # 長期トレンドが逆行 (下降トレンド) の場合は大きく減点
            score -= LONG_TERM_REVERSAL_PENALTY
            long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        else:
            # 長期トレンドが一致 (上昇トレンド) の場合は、ペナルティ回避として扱える
            is_long_term_bullish = True
            
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    tech_data['is_long_term_bullish'] = is_long_term_bullish

    # 2.2. 価格構造/ピボット支持 - ボーナス (最大STRUCTURAL_PIVOT_BONUS)
    structural_pivot_bonus = 0.0
    # BB Lower Bandにタッチ (買われすぎ) & ピボットS1がSL価格に近い
    if last['BBP'] <= 0.05 and pd.notna(last['PP_S1']): # BBPが0に近い (BB Lowerにタッチ)
         # S1がSL価格の±0.5%以内にある場合 (サポートとして機能している)
         sl_s1_diff_percent = abs(stop_loss_price - last['PP_S1']) / last['PP_S1']
         if sl_s1_diff_percent < 0.005: 
             structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
             score += structural_pivot_bonus
             
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    
    # 2.3. モメンタム (RSI/MACD) - ペナルティ/回避ボーナス
    macd_penalty_value = 0.0
    
    # MACDがシグナルラインをデッドクロスしている、またはヒストグラムがマイナスから拡大している場合
    if pd.notna(last['MACD']) and pd.notna(last['MACD_Signal']) and pd.notna(last['MACD_Hist']):
        # デッドクロス（MACD < Signal）かつゼロライン以下
        if last['MACD'] < last['MACD_Signal'] and last['MACD'] < 0:
            macd_penalty_value = MACD_CROSS_PENALTY 
            score -= macd_penalty_value
    
    # RSIが過熱状態の場合（ロングシグナルとしては不利）
    if pd.notna(last['RSI']) and last['RSI'] > 70:
        macd_penalty_value = max(macd_penalty_value, MACD_CROSS_PENALTY * 0.5) # 半分のペナルティを追加
        score -= MACD_CROSS_PENALTY * 0.5
        
    tech_data['macd_penalty_value'] = macd_penalty_value
    
    # 2.4. 出来高 (OBV) - ボーナス (最大OBV_MOMENTUM_BONUS)
    obv_momentum_bonus_value = 0.0
    # OBVがSMAの上で上昇傾向にある場合
    if pd.notna(last['OBV']) and pd.notna(last['OBV_SMA']):
        # OBVがSMAより上にあり、かつ直近2期間で上昇している
        if last['OBV'] > last['OBV_SMA'] and last['OBV'] > df.iloc[-2]['OBV']:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
            score += obv_momentum_bonus_value
            
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # 2.5. ボラティリティ過熱ペナルティ
    volatility_penalty_value = 0.0
    # BB幅 (BBW) が過去平均と比較して非常に大きい場合 (ボラティリティ過熱)
    if pd.notna(last['BBW']):
        # BBWの移動平均を計算し、直近のBBWと比較 (過去10期間)
        bbw_avg = df['BBW'].iloc[-20:-1].mean()
        if bbw_avg > 0:
            bbw_change = (last['BBW'] - bbw_avg) / bbw_avg
            if bbw_change > VOLATILITY_BB_PENALTY_THRESHOLD:
                # 0.5%以上ボラティリティが高い場合はペナルティ
                penalty_factor = min(bbw_change / 0.05, 1.0) # 最大ペナルティはLONG_TERM_REVERSAL_PENALTYの半分
                volatility_penalty_value = -(LONG_TERM_REVERSAL_PENALTY * 0.5 * penalty_factor)
                score += volatility_penalty_value 
            
    tech_data['volatility_penalty_value'] = volatility_penalty_value
    
    # 2.6. 流動性 (板の厚み) ボーナス
    liquidity_bonus_value = 0.0
    # RRRが高いほどロットサイズが小さくなるため、RRRが高い場合はボーナスを付与（最大LIQUIDITY_BONUS_MAX）
    # RRRが2.0の場合にフルボーナス、1.0の場合にゼロ、3.0以上の場合はペナルティなし
    if rr_ratio >= 2.0:
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
        score += liquidity_bonus_value
    elif rr_ratio > 1.0:
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX * (rr_ratio - 1.0) / 1.0
        score += liquidity_bonus_value
        
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    # 2.7. マクロ要因 (FGI/為替)
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    score += sentiment_fgi_proxy_bonus
    score += forex_bonus
    
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    tech_data['forex_bonus'] = forex_bonus
    
    # 2.8. スコアの最終調整とクリッピング (0.0から1.0の間に収める)
    score = max(0.0, min(1.0, score))
    
    # ====================================================================
    # 3. 結果の整形
    # ====================================================================
    
    # 動的なロットサイズ計算: RRRに基づいてリスクを固定する
    # 目標リスク額 (例: BASE_TRADE_SIZE_USDT の 1%)
    target_risk_usdt = BASE_TRADE_SIZE_USDT * 0.01 
    
    # SL価格までのリスク幅 (USDT)
    risk_price_diff = current_price - stop_loss_price
    
    if risk_price_diff <= 0: # 念のため再チェック
        return None

    # 許容リスク額から計算される投入USDT額
    calculated_lot_size_usdt = target_risk_usdt * (current_price / risk_price_diff)
    
    # 最大ロットをBASE_TRADE_SIZE_USDTの5倍に制限 (安全対策)
    max_lot_size = BASE_TRADE_SIZE_USDT * 5.0 
    lot_size_usdt = min(calculated_lot_size_usdt, max_lot_size)
    
    # 最低ロットを10 USDTに制限 (取引所最小額を考慮)
    min_lot_size = 10.0
    lot_size_usdt = max(lot_size_usdt, min_lot_size)
    
    # 最終シグナルデータ
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': score,
        'entry_price': current_price,
        'stop_loss': stop_loss_price,
        'take_profit': take_profit_price,
        'rr_ratio': rr_ratio,
        'lot_size_usdt': lot_size_usdt,
        'tech_data': tech_data,
        'timestamp': datetime.now(JST).isoformat()
    }

    return signal_data


def get_current_signal_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づき、現在の取引閾値を動的に決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # 市場活発 (FGIが高い)
    if fgi_proxy > FGI_ACTIVE_THRESHOLD:
        logging.info(f"🌍 市場環境スコア: FGI={macro_context.get('fgi_raw_value', 'N/A')} (+{fgi_proxy*100:.2f}点), 動的閾値: {SIGNAL_THRESHOLD_ACTIVE*100:.1f}点 (活発)")
        return SIGNAL_THRESHOLD_ACTIVE
        
    # 市場低迷 (FGIが低い)
    elif fgi_proxy < FGI_SLUMP_THRESHOLD:
        logging.info(f"🌍 市場環境スコア: FGI={macro_context.get('fgi_raw_value', 'N/A')} ({fgi_proxy*100:.2f}点), 動的閾値: {SIGNAL_THRESHOLD_SLUMP*100:.1f}点 (低迷)")
        return SIGNAL_THRESHOLD_SLUMP
        
    # 通常
    else:
        logging.info(f"🌍 市場環境スコア: FGI={macro_context.get('fgi_raw_value', 'N/A')} ({fgi_proxy*100:.2f}点), 動的閾値: {SIGNAL_THRESHOLD_NORMAL*100:.1f}点 (通常)")
        return SIGNAL_THRESHOLD_NORMAL

# ====================================================================================
# MAIN LOGIC & LOOP
# ====================================================================================

async def process_symbol(symbol: str, macro_context: Dict) -> List[Dict]:
    """単一の銘柄に対して複数の時間軸で分析を実行する"""
    signals = []
    
    # 各時間軸でOHLCVを取得し、分析を実行
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
        
        # 1. OHLCVデータの取得
        df = await fetch_ohlcv_with_fallback(symbol, tf, limit=limit)
        
        if df is None or len(df) < 50: # 最低50本は必要
            logging.debug(f"ℹ️ {symbol} ({tf}): データ不足 ({len(df) if df is not None else 0}本)。分析をスキップします。")
            continue
            
        # 2. テクニカル指標の計算
        df = calculate_technical_indicators(df)

        # 3. シグナルスコアリング
        signal = apply_analysis_score(df, tf, symbol, macro_context)
        
        if signal:
            signals.append(signal)

    return signals

async def find_long_signals(macro_context: Dict) -> List[Dict]:
    """全ての監視銘柄でロングシグナルを探す"""
    
    if not CURRENT_MONITOR_SYMBOLS:
        logging.warning("⚠️ 監視対象銘柄リストが空です。")
        return []
        
    tasks = [process_symbol(symbol, macro_context) for symbol in CURRENT_MONITOR_SYMBOLS]
    
    # 全ての分析タスクを並行実行
    results = await asyncio.gather(*tasks)
    
    # 結果をフラット化
    all_signals = [signal for sublist in results for signal in sublist]
    
    return all_signals

async def monitor_positions_loop():
    """保有ポジションのSL/TPを監視し、トリガーされたら決済する"""
    global OPEN_POSITIONS
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        
        if not OPEN_POSITIONS:
            continue
            
        positions_to_remove = []
        positions_to_update = []
        
        # 現在価格の取得タスクを準備
        symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
        
        # 並行してティッカー情報を取得
        if EXCHANGE_CLIENT:
            tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        else:
            tickers = {}

        for position in OPEN_POSITIONS:
            symbol = position['symbol']
            
            # 1. 現在価格の確認
            if symbol in tickers and 'last' in tickers[symbol]:
                current_price = tickers[symbol]['last']
                
                # 2. SL/TPのチェック
                exit_type = None
                
                # SLチェック (価格がSLを下回った場合)
                if current_price <= position['stop_loss']:
                    exit_type = "SL (Stop Loss)"
                    
                # TPチェック (価格がTPを上回った場合)
                elif current_price >= position['take_profit']:
                    exit_type = "TP (Take Profit)"
                    
                if exit_type and not TEST_MODE:
                    # 決済実行
                    logging.warning(f"🚨 {symbol}: {exit_type} トリガー! 価格: {current_price:.4f}")
                    
                    # 決済時の損益計算 (テストモードでない場合のみ実行)
                    try:
                        exit_result = await execute_exit_trade(position)
                        
                        if exit_result['status'] == 'ok':
                            exit_price = exit_result['exit_price']
                            pnl_usdt = exit_result['filled_usdt'] - position['filled_usdt']
                            pnl_rate = (pnl_usdt / position['filled_usdt']) if position['filled_usdt'] else 0.0
                            
                            trade_log = {
                                'symbol': symbol,
                                'timeframe': position['timeframe'],
                                'score': position['score'],
                                'rr_ratio': position['rr_ratio'],
                                'filled_amount': position['filled_amount'],
                                'entry_price': position['entry_price'],
                                'stop_loss': position['stop_loss'],
                                'take_profit': position['take_profit'],
                                'exit_price': exit_price,
                                'pnl_usdt': pnl_usdt,
                                'pnl_rate': pnl_rate,
                            }
                            
                            # 決済通知
                            msg = format_telegram_message(
                                position, 
                                "ポジション決済", 
                                SIGNAL_THRESHOLD_NORMAL, # 決済時は閾値は関係ないが、形式として
                                trade_result=trade_log, 
                                exit_type=exit_type
                            )
                            await send_telegram_notification(msg)
                            log_signal(trade_log, "Trade Exit", trade_log)
                            
                            positions_to_remove.append(position)
                            
                        else:
                            logging.error(f"❌ 決済失敗 ({symbol}): {exit_result.get('error_message')}")
                            # 決済失敗時も、次回再トライのためポジションは保持
                            pass 

                    except Exception as e:
                        logging.critical(f"❌ ポジション決済中の致命的なエラー ({symbol}): {e}")
                        positions_to_remove.append(position) # エラー防止のため削除

                elif exit_type and TEST_MODE:
                    # テストモードではログと削除のみ
                    logging.warning(f"⚠️ TEST_MODE: {symbol} - {exit_type} トリガーをシミュレート (決済せず削除)")
                    positions_to_remove.append(position)

        
        # ポジションリストの更新
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
        
async def main_loop():
    """メインの分析・取引実行ループ"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, GLOBAL_MACRO_CONTEXT
    
    # 初回のみ市場データを強制更新
    await update_symbols_by_volume()

    while True:
        start_time = time.time()
        logging.info("--- メイン分析ループ開始 ---")

        try:
            # 1. 銘柄リストを更新 (1時間ごと)
            if time.time() - LAST_SUCCESS_TIME > 60 * 60:
                 await update_symbols_by_volume()

            # 2. グローバルマクロコンテキストの取得
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            current_threshold = get_current_signal_threshold(GLOBAL_MACRO_CONTEXT)

            # 3. シグナル検索タスクを実行
            tasks = [process_symbol(symbol, GLOBAL_MACRO_CONTEXT) for symbol in CURRENT_MONITOR_SYMBOLS]
            results = await asyncio.gather(*tasks)
            all_signals = [signal for sublist in results for signal in sublist]
            
            # 4. スコアに基づいてソートし、分析結果を保存
            sorted_signals = sorted(all_signals, key=lambda s: s['score'], reverse=True)
            LAST_ANALYSIS_SIGNALS = sorted_signals 

            # 5. 取引シグナルのフィルタリングと実行
            
            # 閾値とRRR基準を満たしたシグナルを抽出
            tradable_signals = [
                s for s in sorted_signals 
                if s['score'] >= current_threshold and s['rr_ratio'] >= 1.0
            ]
            
            # クールダウンと未保有ポジションをチェック
            new_trade_signals = []
            for signal in tradable_signals:
                symbol = signal['symbol']
                
                # クールダウンチェック
                if time.time() - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"ℹ️ {symbol} はクールダウン期間中です。シグナルをスキップします。")
                    continue
                
                # 保有ポジションチェック
                if symbol in [p['symbol'] for p in OPEN_POSITIONS]:
                    logging.info(f"ℹ️ {symbol} は既にポジションを保有しています。シグナルをスキップします。")
                    continue
                    
                new_trade_signals.append(signal)

            
            # TOP_SIGNAL_COUNT個のシグナルを取引
            for signal in new_trade_signals[:TOP_SIGNAL_COUNT]:
                symbol = signal['symbol']
                lot_size_usdt = signal['lot_size_usdt']

                logging.warning(f"🎯 取引シグナル発見: {symbol} ({signal['timeframe']}), スコア: {signal['score'] * 100:.2f}")

                trade_result = await execute_trade(symbol, lot_size_usdt)

                if trade_result['status'] == 'ok':
                    # 取引成功した場合、ポジション管理リストに追加
                    filled_amount = trade_result['filled_amount']
                    filled_usdt = trade_result['filled_usdt']
                    entry_price = trade_result['entry_price']
                    
                    new_position = signal.copy()
                    new_position['filled_amount'] = filled_amount
                    new_position['filled_usdt'] = filled_usdt
                    new_position['entry_price'] = entry_price # 実際の約定価格で更新
                    
                    OPEN_POSITIONS.append(new_position)

                # 通知とロギング (成功・失敗に関わらず)
                msg = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                await send_telegram_notification(msg)
                log_signal(signal, "Trade Signal", trade_result)

                # クールダウン時間を更新 (取引を試みた場合)
                LAST_SIGNAL_TIME[symbol] = time.time()
                
            
            # 6. 初回起動通知の送信
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                account_status = await fetch_account_status()
                startup_msg = format_startup_message(
                    account_status,
                    GLOBAL_MACRO_CONTEXT,
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    "v19.0.28 - Safety and Frequency Finalized (Patch 36)"
                )
                await send_telegram_notification(startup_msg)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            LAST_SUCCESS_TIME = time.time()

        except Exception as e:
            logging.critical(f"❌ メインループで致命的なエラー: {type(e).__name__} - {e}", exc_info=True)
            # ログにエラーを記録し、ループを継続
            
        end_time = time.time()
        elapsed = end_time - start_time
        wait_time = max(0, LOOP_INTERVAL - elapsed)
        
        logging.info(f"--- メイン分析ループ完了。所要時間: {elapsed:.2f}秒。次回実行まで {wait_time:.0f}秒待機。 ---")
        await asyncio.sleep(wait_time)


async def analysis_only_notification_loop():
    """1時間ごとの分析専用通知ループ"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    while True:
        await asyncio.sleep(10) # 頻繁にチェックしない
        
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            continue
            
        current_time = time.time()
        
        if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            
            # メインループで取得・保存された最新の結果を使用
            if not LAST_ANALYSIS_SIGNALS:
                 logging.info("ℹ️ 分析結果がないため、時間ごとの分析通知をスキップします。")
                 LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time
                 continue

            current_threshold = get_current_signal_threshold(GLOBAL_MACRO_CONTEXT)
            
            # 通知メッセージを作成・送信
            msg = format_analysis_only_message(
                LAST_ANALYSIS_SIGNALS, 
                GLOBAL_MACRO_CONTEXT, 
                current_threshold, 
                len(CURRENT_MONITOR_SYMBOLS)
            )
            await send_telegram_notification(msg)
            
            # ログ記録
            if LAST_ANALYSIS_SIGNALS:
                log_signal(LAST_ANALYSIS_SIGNALS[0], "Hourly Analysis")
            
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

async def webshare_upload_loop():
    """ログファイルアップロードループ"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    if not WEBSHARE_HOST:
        logging.info("ℹ️ WEBSHARE HOSTが未設定のため、ログアップロードループを起動しません。")
        return
        
    while True:
        await asyncio.sleep(60 * 15) # 15分ごとにチェック
        
        current_time = time.time()
        
        if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            await upload_logs_to_webshare()
            LAST_WEBSHARE_UPLOAD_TIME = current_time


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントを初期化
    if not await initialize_exchange_client():
        logging.critical("❌ BOTを終了します。CCXTクライアント初期化失敗。")
        sys.exit(1)
        
    # 2. 初回マクロコンテキストを取得
    global GLOBAL_MACRO_CONTEXT
    GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
    
    # 3. ループの初回実行タイミングを設定 (起動後すぐに実行されないように調整)
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    # 初回起動後1分後にメインループを実行
    MAIN_LOOP_DELAY_SECONDS = 60 * 1
    LAST_SUCCESS_TIME = time.time() - (LOOP_INTERVAL - MAIN_LOOP_DELAY_SECONDS) 

    # 初回起動後15分後に分析通知 (ANALYSIS_ONLY_INTERVALより短いインターバルに設定)
    ANALYSIS_ONLY_DELAY_SECONDS = 60 * 15 
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL - ANALYSIS_ONLY_DELAY_SECONDS) 
    
    # 初回起動後30分後にWebShareアップロード
    WEBSHARE_UPLOAD_DELAY_SECONDS = 60 * 30 
    LAST_WEBSHARE_UPLOAD_TIME = time.time() - (WEBSHARE_UPLOAD_INTERVAL - WEBSHARE_UPLOAD_DELAY_SECONDS)

    # 4. メインの取引ループと監視ループを起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop()) 
    asyncio.create_task(webshare_upload_loop()) 
    asyncio.create_task(monitor_positions_loop()) 


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
        "bot_version": "v19.0.28 - Safety and Frequency Finalized (Patch 36)", # バージョン更新
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "test_mode": TEST_MODE,
        "is_first_main_loop_completed": IS_FIRST_MAIN_LOOP_COMPLETED,
    }
    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    # Render環境で必要なポート設定
    port = int(os.environ.get("PORT", 8080))
    # ローカル実行時は 10000 を使用することが多いため、デフォルトで 10000 に設定
    if port == 8080: 
        port = 10000 
        
    logging.info(f"==> Running 'uvicorn main_render:app --host 0.0.0.0 --port {port}'")
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level="info")
