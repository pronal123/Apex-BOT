# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.67, 0.63, 0.58) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
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
STRUCTURAL_PIVOT_BONUS = 0.05       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - Patch 36確定)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: 頻度目標達成のため閾値を引き下げ (この値で確定)
SIGNAL_THRESHOLD_SLUMP = 0.67       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.63      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.58      # 活発時の閾値 (3+銘柄/日を想定)

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
        fgi_normalized = (fgi_value - 50) / 20.0 
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
                    if usdt_value >= 10.0: 
                        status['open_positions'].append({
                            'symbol': symbol, 
                            'amount': info, 
                            'usdt_value': usdt_value
                        })

                except Exception as e_ticker:
                    logging.warning(f"⚠️ {symbol} の価格取得中にエラーが発生しました: {e_ticker}")
                    continue 
            
    except Exception as e:
        status['error'] = str(e)
        logging.error(f"❌ 口座ステータス取得エラー（メイン処理）: {e}")

    return status

# ====================================================================================
# STRATEGY & SCORING LOGIC (エラー修正のため再構築)
# ====================================================================================

# 💡 【欠落していた関数】ボラティリティのスコア計算
def calculate_volatility_score(df: pd.DataFrame) -> float:
    """ボラティリティの過熱度に基づいてスコアペナルティを計算する (BBands利用)"""
    
    # ボリンジャーバンド (20, 2) を計算
    df.ta.bbands(close='close', length=20, std=2, append=True)
    
    # 最新のローソク足を取得
    if df.empty: return 0.0
    latest = df.iloc[-1]
    
    # BBandsが計算できていない場合は0.0を返す
    if 'BBL_20_2.0' not in df.columns:
        return 0.0

    # BBWP (Bandwidth Percent) を計算
    bbw = latest['BBU_20_2.0'] - latest['BBL_20_2.0']
    bbm = latest['BBM_20_2.0'] # 中心の移動平均 (SMA 20)
    
    # バンド幅の比率 (ボラティリティの強さ)
    if bbm == 0: return 0.0
    bbwp_ratio = bbw / bbm 
    
    penalty = 0.0
    
    # VOLATILITY_BB_PENALTY_THRESHOLD (0.01) を基準
    # 2%を超えるボラティリティを過熱と見なす
    if bbwp_ratio > VOLATILITY_BB_PENALTY_THRESHOLD * 2: 
        penalty = -0.10 # 10点ペナルティ
    # 1%を超えるボラティリティを警戒
    elif bbwp_ratio > VOLATILITY_BB_PENALTY_THRESHOLD: 
        penalty = -0.05 # 5点ペナルティ
        
    return penalty


# 💡 【欠落していた関数】流動性ボーナス計算 (非同期)
async def calculate_liquidity_bonus(symbol: str, price: float) -> Tuple[float, float]:
    """
    板情報から現在の流動性(板の厚み)を評価し、ボーナスを計算する
    (ロング戦略なので、ASK側の薄さ/BID側の厚みを評価)
    """
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return 0.0, 0.0

    try:
        # 1. 板情報の取得 (深度10程度で十分)
        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=10)
        
        # 2. 評価価格帯の定義 (現在価格の±0.5%を評価)
        RANGE_PERCENT = 0.005 
        price_range_bid = price * (1 - RANGE_PERCENT) # 0.5%下の価格
        price_range_ask = price * (1 + RANGE_PERCENT) # 0.5%上の価格
        
        # 3. BID側（サポート）の累積USDTとASK側（抵抗）の累積USDTを計算
        cumulative_bid_usdt = 0.0
        for bid_price, amount in orderbook['bids']:
            if bid_price < price_range_bid:
                break 
            cumulative_bid_usdt += bid_price * amount

        cumulative_ask_usdt = 0.0
        for ask_price, amount in orderbook['asks']:
            if ask_price > price_range_ask:
                break 
            cumulative_ask_usdt += ask_price * amount

        # 4. 流動性比率の計算 (BIDの厚み / ASKの厚み)
        # ロングシグナルでは BID > ASK が望ましい (下にサポートが厚い)
        liquidity_ratio = cumulative_bid_usdt / (cumulative_ask_usdt if cumulative_ask_usdt > 1 else 1) 
        
        # 5. ボーナスの計算
        # 閾値: 比率1.5以上でフルボーナス (MAX_LIQUIDITY_BONUS = 0.06)
        if liquidity_ratio >= 1.5:
            bonus = LIQUIDITY_BONUS_MAX
        elif liquidity_ratio > 1.0: # 1.0から1.5の間で線形補間
            bonus = LIQUIDITY_BONUS_MAX * ((liquidity_ratio - 1.0) / 0.5)
        else:
            bonus = 0.0 
            
        # ボーナスは最大値に制限
        final_bonus = min(LIQUIDITY_BONUS_MAX, bonus)
        
        return final_bonus, liquidity_ratio
        
    except Exception as e:
        # 価格取得エラーなどで板情報が取れない場合はボーナス無し
        logging.debug(f"⚠️ 流動性ボーナス計算エラー {symbol}: {e}")
        return 0.0, 0.0


# 💡 【欠落していた関数】スコアリングロジック (コア機能)
def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    単一の時間足データに対してテクニカル分析とスコアリングを実行する。
    ロングシグナル候補（スコア、SL、TP、RRR）を計算して返す。
    """
    # データの十分性チェック
    if df is None or len(df) < LONG_TERM_SMA_LENGTH + 50:
        return None 
        
    # 1. テクニカル指標の計算
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # 長期SMA (200)
    df.ta.rsi(append=True) # RSI (14)
    df.ta.macd(append=True) # MACD (12, 26, 9)
    df.ta.obv(append=True) # OBV
    df.ta.bbands(append=True) # BBands
    
    # 最新のデータポイント
    latest = df.iloc[-1]
    
    # 指標の存在チェック
    if f'SMA_{LONG_TERM_SMA_LENGTH}' not in df.columns or 'RSI_14' not in df.columns or 'MACDH_12_26_9' not in df.columns:
        return None
        
    current_close = latest['close']
    
    # 2. ベーススコアの適用
    score = BASE_SCORE # 0.60 (60点)
    tech_data = {'timeframe': timeframe}
    
    # 3. 長期トレンドフィルタ (SMA 200)
    sma_long = latest[f'SMA_{LONG_TERM_SMA_LENGTH}']
    
    # 買いシグナルは価格がSMAより上、またはSMAにタッチ/反発が望ましい
    is_above_sma = current_close > sma_long
    
    # 長期トレンド逆行時のペナルティ (価格がSMAを大きく下回っている場合)
    long_term_reversal_penalty_value = 0.0
    if current_close < sma_long * 0.995: # SMAより0.5%以上下にいる場合は逆行と見なす
        score -= LONG_TERM_REVERSAL_PENALTY # -0.20
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
        
    tech_data['is_above_sma'] = is_above_sma
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # 4. モメンタムとクロスオーバーの確認
    
    # RSIが低すぎないか (ロングのモメンタム候補: 40以上)
    rsi_low = latest['RSI_14'] < RSI_MOMENTUM_LOW
    
    # MACDの状態確認 (MACDラインがシグナルラインを上回っている、またはゴールデンクロス直後)
    macd_h = latest['MACDH_12_26_9']
    is_macd_positive_cross = (macd_h > 0)
    
    # MACDが不利なクロス/発散時のペナルティ
    macd_penalty_value = 0.0
    # MACDがシグナルを下回っている OR RSIがロングに不利なゾーン(40未満)
    if not is_macd_positive_cross or rsi_low:
        score -= MACD_CROSS_PENALTY # -0.15
        macd_penalty_value = MACD_CROSS_PENALTY

    tech_data['rsi_low'] = rsi_low
    tech_data['is_macd_positive_cross'] = is_macd_positive_cross
    tech_data['macd_penalty_value'] = macd_penalty_value
    tech_data['stoch_filter_penalty_value'] = 0.0 # stochフィルターは簡略化のため0.0

    # 5. 価格構造/ピボット支持の確認 (SMAからの反発/支持)
    structural_pivot_bonus = 0.0
    # 価格がSMAの近く(±1%)にあり、かつ現在の足が陽線であれば、支持/反発のボーナスを適用
    if (abs(current_close - sma_long) / sma_long < 0.01) and (current_close > df.iloc[-2]['close']):
         score += STRUCTURAL_PIVOT_BONUS # +0.05
         structural_pivot_bonus = STRUCTURAL_PIVOT_BONUS
         
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    tech_data['volume_confirmation_bonus'] = 0.0 # 出来高単体ボーナスは簡略化のため0.0

    # 6. 出来高/OBVによる確証
    obv_momentum_bonus_value = 0.0
    # OBVの移動平均を計算し、OBVが上向きの場合にボーナス
    obv_ma = df['OBV'].iloc[-10:].mean() # 10期間移動平均
    if latest['OBV'] > obv_ma:
        score += OBV_MOMENTUM_BONUS # +0.04
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # 7. ボラティリティによるスコア調整
    volatility_penalty = calculate_volatility_score(df)
    score += volatility_penalty
    tech_data['volatility_penalty_value'] = volatility_penalty
    
    # 8. マクロ要因の適用 (FGI)
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0)
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    tech_data['forex_bonus'] = 0.0 # 機能削除済み
    
    # 9. 最終チェックとシグナルオブジェクトの生成
    
    # ロングシグナルの最低条件: MACDの不利なクロスがない、または長期トレンドに逆行していない
    is_qualified_for_long = (long_term_reversal_penalty_value == 0.0 and macd_penalty_value == 0.0) or \
                            (is_above_sma and is_macd_positive_cross)

    if not is_qualified_for_long and score < BASE_SCORE:
        # 最低基準を満たさない場合はシグナルを返さない (スコアが低すぎる場合のみ)
        if score < 0.50:
            return None 

    # 最終的なシグナルデータを作成
    signal = {
        'symbol': '', # main_loopで設定
        'timeframe': timeframe,
        'entry_price': current_close,
        'score': max(0.0, score), # スコアがマイナスにならないようにする
        'tech_data': tech_data,
        'stop_loss': 0.0, # calculate_trade_parametersで設定
        'take_profit': 0.0, # calculate_trade_parametersで設定
        'rr_ratio': 0.0, # calculate_trade_parametersで設定
    }
    
    return signal


async def calculate_trade_parameters(df: pd.DataFrame, signal: Dict) -> Optional[Dict]:
    """
    シグナルからエントリー、ストップロス、テイクプロフィット、RRRを計算する
    (ロング/現物取引専用)
    """
    try:
        current_price = signal['entry_price']
        
        # 1. ストップロス (SL) の決定: 直近のサポートレベルを使用
        # ATRに基づいてダイナミックにSLを設定
        df.ta.atr(length=14, append=True)
        if 'ATR_14' not in df.columns or df.iloc[-1]['ATR_14'] is np.nan:
             sl_length = 20 # ATRが計算できない場合は20期間の最安値を使用
        
        # SLレベルの決定 (ATRの1.5倍、または20期間の最安値の0.5%下)
        if 'ATR_14' in df.columns and not df.iloc[-1]['ATR_14'] is np.nan:
            atr_value = df.iloc[-1]['ATR_14']
            stop_loss = current_price - (atr_value * 1.5)
        else:
            min_low = df['low'].iloc[-20:].min()
            stop_loss = min_low * 0.995 # 最安値から0.5%下

        if stop_loss >= current_price:
            return None 

        # 2. リスク幅の計算
        risk_amount = current_price - stop_loss
        if risk_amount <= 0:
             return None 

        # 3. テイクプロフィット (TP) の決定: RRR 1.5をターゲットとする
        target_rr_ratio = 1.5 
        
        # 4H足のみ RR=2.0をターゲットとし、その他の時間足はRR=1.5をターゲットとする
        if signal['timeframe'] == '4h':
             target_rr_ratio = 2.0
        
        reward_amount = risk_amount * target_rr_ratio
        take_profit = current_price + reward_amount
        
        # 4. 最終的なRRRの計算
        final_rr_ratio = (take_profit - current_price) / risk_amount
        
        # 5. シグナルの更新
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        signal['rr_ratio'] = final_rr_ratio
        
        return signal
        
    except Exception as e:
        logging.error(f"❌ 取引パラメータ計算エラー {signal.get('symbol', 'N/A')}: {e}")
        return None

async def process_symbol_analysis(symbol: str, macro_context: Dict) -> List[Dict]:
    """単一銘柄の複数時間足分析を実行する"""
    all_signals: List[Dict] = []
    
    # OHLCVデータの取得と並行処理
    analysis_tasks = []
    for tf in TARGET_TIMEFRAMES:
        analysis_tasks.append(
            fetch_ohlcv_with_fallback(symbol, tf, REQUIRED_OHLCV_LIMITS[tf])
        )

    ohlcv_data = await asyncio.gather(*analysis_tasks)
    
    for i, timeframe in enumerate(TARGET_TIMEFRAMES):
        df = ohlcv_data[i]
        
        # 1. テクニカルスコアリング
        scored_signal = analyze_single_timeframe(df, timeframe, macro_context)
        
        if scored_signal:
            # 2. 流動性ボーナスの計算と適用
            liquidity_bonus, liquidity_ratio = await calculate_liquidity_bonus(symbol, scored_signal['entry_price'])
            scored_signal['score'] += liquidity_bonus
            scored_signal['tech_data']['liquidity_bonus_value'] = liquidity_bonus
            scored_signal['tech_data']['liquidity_ratio'] = liquidity_ratio

            # 3. TP/SL/RRRの計算
            final_signal = await calculate_trade_parameters(df, scored_signal)

            if final_signal:
                final_signal['symbol'] = symbol
                all_signals.append(final_signal)

    return all_signals

# ====================================================================================
# TRADE EXECUTION LOGIC (動的ロット & ポジション管理)
# ====================================================================================

def determine_trade_size(score: float) -> float:
    """スコアに基づいて動的な取引サイズを計算する"""
    
    # スコアの正規化 (0.60をmin、0.80をmaxとして、ロット倍率を0.5から1.5に線形補間)
    
    MIN_SCORE = 0.60
    MAX_SCORE = 0.80
    MIN_MULTIPLIER = 0.5
    MAX_MULTIPLIER = 1.5
    
    # スコアを MIN_SCORE と MAX_SCORE の間でクランプ
    clamped_score = max(MIN_SCORE, min(MAX_SCORE, score))
    
    # スコアに基づく線形補間
    normalized_score = (clamped_score - MIN_SCORE) / (MAX_SCORE - MIN_SCORE)
    
    # ロット倍率の計算 (0.5 から 1.5)
    multiplier = MIN_MULTIPLIER + normalized_score * (MAX_MULTIPLIER - MIN_MULTIPLIER)
    
    # 最終的な取引サイズ
    trade_size = BASE_TRADE_SIZE_USDT * multiplier
    
    logging.info(f"📈 動的ロット計算: Score={score:.3f} -> Multiplier={multiplier:.2f}x -> Size={trade_size:.2f} USDT")
    
    return max(10.0, trade_size) # 最小ロット10 USDTを保証


async def execute_trade_order(signal: Dict) -> Dict:
    """
    現物取引の成行買い注文をCCXTを通じて実行し、成功した場合はポジションを登録する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = signal['symbol']
    trade_size_usdt = signal['lot_size_usdt'] # 動的に計算されたロットサイズを使用
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} 現物ロング注文は実行されません。")
        return {'status': 'ok', 'filled_amount': 0.0, 'filled_usdt': trade_size_usdt, 'is_test': True}
        
    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'Exchange client not initialized.'}

    side = 'buy'
    type = 'market'
    
    try:
        # 1. 注文量の計算
        ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
        price = ticker['ask'] # 買い注文は売り板価格(ask)で量を計算
        
        if not price:
            raise ccxt.ExchangeError(f"Cannot fetch price for {symbol}")
            
        amount = trade_size_usdt / price
        
        # 2. 注文量の精度調整 🚨 金銭的損失を避けるため、極めて重要なステップ 🚨
        market = EXCHANGE_CLIENT.markets[symbol]
        if market and 'precision' in market and 'amount' in market['precision']:
            amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
            
        
        # 3. 注文実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol, 
            type=type, 
            side=side, 
            amount=amount, 
            params={}
        )

        # 4. 約定結果の解析とポジション登録
        filled_amount = order.get('filled', 0.0)
        filled_usdt = filled_amount * order.get('average', price)

        if filled_amount > 0.0:
            logging.info(f"✅ {symbol} ロング注文成功: {filled_amount:.4f} @{order.get('average', price):.6f}")
            
            # ポジションを管理リストに登録
            new_position = {
                'id': str(uuid.uuid4()), # 一意のID
                'symbol': symbol,
                'entry_time': time.time(),
                'entry_price': order.get('average', price),
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'signal_score': signal['score'],
                'timeframe': signal['timeframe'],
                'rr_ratio': signal['rr_ratio'],
            }
            OPEN_POSITIONS.append(new_position)
            
            return {
                'status': 'ok', 
                'filled_amount': filled_amount, 
                'filled_usdt': filled_usdt,
                'entry_price': order.get('average', price),
                'order_id': order.get('id')
            }
        else:
            logging.warning(f"⚠️ {symbol} 注文は成功しましたが、約定量が0です。")
            return {
                'status': 'warning', 
                'filled_amount': 0.0, 
                'filled_usdt': 0.0, 
                'error_message': 'Order successful but filled amount is zero/partial fill only.'
            }

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引実行エラー - 残高不足: {symbol} - {e}")
        return {'status': 'error', 'error_message': 'Insufficient Funds.'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引実行エラー - 取引所エラー: {symbol} - {e}")
        return {'status': 'error', 'error_message': f'Exchange Error: {e}'}
    except Exception as e:
        logging.error(f"❌ 取引実行エラー - 予期せぬエラー: {symbol} - {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'Unexpected Error: {e}'}


async def execute_closing_trade(position: Dict, exit_type: str) -> Dict:
    """
    ポジションを成行売却で決済する。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    amount_to_sell = position['filled_amount']
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} 現物売却注文は実行されません。")
        return {'status': 'ok', 'exit_price': position['entry_price'] * (1.001 if exit_type == 'TP (Take Profit)' else 0.999), 'filled_amount': amount_to_sell, 'is_test': True}
        
    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'Exchange client not initialized.'}

    side = 'sell'
    type = 'market'
    
    try:
        # 1. 注文量の精度調整 🚨 決済数量の丸めエラーは残高誤差に直結するため重要 🚨
        market = EXCHANGE_CLIENT.markets[symbol]
        if market and 'precision' in market and 'amount' in market['precision']:
            amount_to_sell = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_to_sell)
            
        # 2. 注文実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol, 
            type=type, 
            side=side, 
            amount=amount_to_sell, 
            params={}
        )

        # 3. 約定結果の解析
        filled_amount = order.get('filled', 0.0)
        exit_price = order.get('average', 0.0)
        
        if filled_amount > 0.0 and exit_price > 0.0:
            pnl_usdt = (exit_price - position['entry_price']) * filled_amount
            pnl_rate = pnl_usdt / position['filled_usdt']
            
            logging.info(f"✅ {symbol} 決済成功 ({exit_type}): {filled_amount:.4f} @{exit_price:.6f}. PnL: {pnl_usdt:.2f} USDT")
            
            return {
                'status': 'ok', 
                'exit_price': exit_price,
                'filled_amount': filled_amount, 
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'entry_price': position['entry_price'],
                'stop_loss': position['stop_loss'], # 決済通知用に含める
                'take_profit': position['take_profit'], # 決済通知用に含める
                'order_id': order.get('id')
            }
        else:
            logging.error(f"❌ {symbol} 決済注文は成功しましたが、約定できませんでした。")
            return {'status': 'error', 'error_message': 'Order filled amount is zero.'}

    except Exception as e:
        logging.error(f"❌ ポジション決済エラー ({exit_type}): {symbol} - {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'Exit Error: {e}'}

# ====================================================================================
# MAIN LOOPS & SCHEDULER
# ====================================================================================

def determine_dynamic_threshold(macro_context: Dict) -> float:
    """マクロコンテキストに基づいて動的な取引閾値を決定する"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP # 67点 (低迷時)
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE # 58点 (活発時)
    else:
        return SIGNAL_THRESHOLD_NORMAL # 63点 (通常時)


async def main_loop():
    """定期的に実行されるメインの取引分析・実行ループ"""
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    
    while True:
        start_time = time.time()
        logging.info("🚀 メイン分析ループを開始します...")
        
        try:
            # 1. 市場コンテキストの更新
            await update_symbols_by_volume()
            GLOBAL_MACRO_CONTEXT = await get_crypto_macro_context()
            current_threshold = determine_dynamic_threshold(GLOBAL_MACRO_CONTEXT)

            # 💡 初回起動完了通知
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                account_status = await fetch_account_status()
                startup_message = format_startup_message(
                    account_status,
                    GLOBAL_MACRO_CONTEXT,
                    len(CURRENT_MONITOR_SYMBOLS),
                    current_threshold,
                    "v19.0.28 - Safety and Frequency Finalized (Patch 36)" # バージョン更新
                )
                await send_telegram_notification(startup_message)
                logging.info("🚀 初回メインループ完了。起動完了通知を送信しました。")
                IS_FIRST_MAIN_LOOP_COMPLETED = True 

            # 2. 全銘柄の分析を並行処理
            analysis_tasks = [
                process_symbol_analysis(symbol, GLOBAL_MACRO_CONTEXT)
                for symbol in CURRENT_MONITOR_SYMBOLS
            ]
            all_results_list = await asyncio.gather(*analysis_tasks)
            
            # 結果をフラット化
            all_signals = [signal for results in all_results_list for signal in results]
            LAST_ANALYSIS_SIGNALS = all_signals

            # 3. シグナル候補の選定と実行 (RRR >= 1.0, Score >= Dynamic Threshold)
            qualified_signals = [
                s for s in all_signals 
                if s['rr_ratio'] >= 1.0 and s['score'] >= current_threshold
            ]
            qualified_signals.sort(key=lambda s: s['score'], reverse=True)
            
            # 4. 取引実行と通知
            notified_count = 0
            for signal in qualified_signals:
                symbol = signal['symbol']
                current_time = time.time()
                
                # クールダウンチェック
                if current_time - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"ℹ️ {symbol} はクールダウン期間中です。取引をスキップします。")
                    continue 
                
                # 💡 既に管理ポジションがある場合はスキップ (同一銘柄の多重エントリー防止)
                if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                    logging.info(f"ℹ️ {symbol} は既に管理中のポジションがあるため、取引をスキップします。")
                    continue

                if notified_count < TOP_SIGNAL_COUNT:
                    
                    # 4.1. 動的ロットサイズの計算
                    dynamic_trade_size = determine_trade_size(signal['score'])
                    signal['lot_size_usdt'] = dynamic_trade_size
                    
                    # 4.2. 取引実行ロジック
                    trade_result = await execute_trade_order(signal)
                    
                    # 4.3. 通知とログ
                    message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
                    await send_telegram_notification(message)
                    log_signal(signal, log_type="TRADE_SIGNAL", trade_result=trade_result)

                    # 状態更新
                    LAST_SIGNAL_TIME[symbol] = current_time
                    notified_count += 1
            
            # 5. 低スコア/シグナルゼロ時のロギング
            # ... (低スコア/シグナルゼロ時のロギングロジックは分析通知ループに統合されるため、ここでは省略)

            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            logging.error(f"❌ メインループ処理中にエラーが発生しました: {e}", exc_info=True)

        # 次の実行までの待機
        elapsed_time = time.time() - start_time
        sleep_duration = max(0, LOOP_INTERVAL - elapsed_time)
        logging.info(f"💤 メインループを {sleep_duration:.2f} 秒間休止します。")
        await asyncio.sleep(sleep_duration)


async def monitor_positions_loop():
    """オープンポジションのSL/TP監視と決済を実行するループ (10秒ごと)"""
    global OPEN_POSITIONS
    
    while True:
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒待機
        
        if not OPEN_POSITIONS:
            logging.debug("ℹ️ 監視中のポジションはありません。")
            continue
            
        logging.info(f"🔍 ポジション監視ループを開始します。対象: {len(OPEN_POSITIONS)} 銘柄")
        
        positions_to_remove = []
        
        # ポジションのシンボルを抽出
        monitoring_symbols = [p['symbol'] for p in OPEN_POSITIONS]
        
        try:
            # 監視中の全銘柄の最新価格を一度に取得 (効率化)
            tickers = await EXCHANGE_CLIENT.fetch_tickers(monitoring_symbols)
        except Exception as e:
            logging.error(f"❌ ポジション監視中の価格取得エラー: {e}")
            continue
            
        for position in OPEN_POSITIONS:
            symbol = position['symbol']
            stop_loss = position['stop_loss']
            take_profit = position['take_profit']
            
            current_price = tickers.get(symbol, {}).get('last')
            
            if current_price is None:
                logging.warning(f"⚠️ {symbol} の最新価格を取得できませんでした。監視を続行します。")
                continue
                
            exit_type = None
            if current_price <= stop_loss:
                exit_type = "SL (Stop Loss)"
            elif current_price >= take_profit:
                exit_type = "TP (Take Profit)"
            
            if exit_type:
                logging.warning(f"🚨 決済トリガー: {symbol} が {exit_type} に到達 (Current: {current_price:.6f})")
                
                # 決済実行
                exit_result = await execute_closing_trade(position, exit_type)
                
                # 決済通知とログ
                exit_data = {
                    'symbol': symbol,
                    'timeframe': position['timeframe'],
                    'score': position['signal_score'],
                    'rr_ratio': position['rr_ratio'],
                    'entry_price': position['entry_price'],
                    'stop_loss': stop_loss,
                    'take_profit': take_profit,
                }
                
                # ログを記録
                log_signal(exit_data, log_type="TRADE_EXIT", trade_result=exit_result)
                
                # 通知を送信
                notification_message = format_telegram_message(
                    exit_data, 
                    "ポジション決済", 
                    determine_dynamic_threshold(GLOBAL_MACRO_CONTEXT), 
                    trade_result=exit_result, 
                    exit_type=exit_type
                )
                await send_telegram_notification(notification_message)

                positions_to_remove.append(position['id'])
                
        # 決済が完了したポジションをリストから削除
        if positions_to_remove:
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] not in positions_to_remove]
            logging.info(f"🗑️ {len(positions_to_remove)} 件のポジションを管理リストから削除しました。")


async def analysis_only_notification_loop():
    """1時間ごとに分析専用の市場スナップショットを通知するループ"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT
    
    while True:
        now = time.time()
        
        if now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
            logging.info("⏳ 1時間ごとの分析専用通知を実行します...")
            
            current_threshold = determine_dynamic_threshold(GLOBAL_MACRO_CONTEXT)
            
            if not LAST_ANALYSIS_SIGNALS:
                 logging.info("📝 前回のメインループで分析結果が取得されていませんでしたが、分析なしの通知を試行します。")
            
            notification_message = format_analysis_only_message(
                LAST_ANALYSIS_SIGNALS, 
                GLOBAL_MACRO_CONTEXT,
                current_threshold,
                len(CURRENT_MONITOR_SYMBOLS)
            )
            
            await send_telegram_notification(notification_message)
            # ログ記録時には、analysis_only_notification_loopで取得された最新のanalysis_signalsを使用
            log_signal({'signals': LAST_ANALYSIS_SIGNALS}, log_type="HOURLY_ANALYSIS") 

            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = now
            
        await asyncio.sleep(60) 


async def webshare_upload_loop():
    """1時間ごとにログファイルをWebShare (FTP) にアップロードするループ"""
    global LAST_WEBSHARE_UPLOAD_TIME
    
    while True:
        now = time.time()
        
        if now - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            logging.info("⏳ WebShareログアップロードを実行します...")
            await upload_logs_to_webshare()
            LAST_WEBSHARE_UPLOAD_TIME = now
            
        await asyncio.sleep(60) 

# ====================================================================================
# FASTAPI / ENTRY POINT
# ====================================================================================

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        sys.exit(1)

    # 2. 初期状態の更新 (銘柄リスト更新)
    await update_symbols_by_volume()

    # 3. グローバル変数の初期化
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()
    
    # 初回起動後15分後に分析通知
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
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_test_mode": TEST_MODE,
        "is_first_loop_completed": IS_FIRST_MAIN_LOOP_COMPLETED,
    }
    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    pass
