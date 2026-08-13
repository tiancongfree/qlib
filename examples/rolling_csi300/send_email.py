import smtplib
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from getpass import getpass

def send_email(stock_code, actions, action_ratios=None, send_flag=True,
               sender="2538438670@qq.com",
               receiver="1163404434@qq.com",
               subject="Python 自动发送邮件测试",
               text_body="这是纯文本内容，Hello from Python!",
               html_body=None,
               attachment_path=None,
               password=None):
    """
    stock_code: 股票代码字符串
    actions: 操作列表，如 ["BUY 2024-07-28 3000.00", "SELL 2024-07-29 3050.00"]
    send_flag: 是否发送邮件
    """
    if not send_flag:
        print("未触发邮件发送。")
        return

    if action_ratios is None:
        action_ratios = [None] * len(actions)
    elif len(action_ratios) != len(actions):
        raise ValueError("action_quants length must match actions length")

    # 构造 HTML 内容
    html_body = html_body or f"""
    <html>
      <body>
        <h3>操作列表</h3>
        <table border="1" cellpadding="5" cellspacing="0">
          <tr>
            <th>股票代码</th>
            <th>操作</th>
            <th>交易金额占比</th>
          </tr>
          {''.join(
              f'<tr><td>{stock_code}</td><td>{action}</td><td>{("" if ratio is None else f"{ratio*100:.2f}%")}</td></tr>'
              for action, ratio in zip(actions, action_ratios)
          )}
        </table>
      </body>
    </html>
    """

    password = password or os.getenv("EMAIL_PASSWORD")
    if not password:
        print("未设置环境变量 EMAIL_PASSWORD，将提示输入密码...")
        password = getpass("输入邮箱密码: ")

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = receiver
    msg["Subject"] = subject
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={os.path.basename(attachment_path)}"
            )
            msg.attach(part)

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, receiver, msg.as_string())
        print("邮件发送成功！")
    except Exception as e:
        if str(e) == "(-1, b'\\x00\\x00\\x00')":
            print("邮件已成功发送（忽略QQ邮箱连接关闭异常）")
        else:
            raise

def _parse_action(action_str):
    """从 last_action 中解析方向标签和价格"""
    if not action_str:
        return '无操作', 0.0
    parts = action_str.split()
    prefix = parts[0] if parts else ''
    price = float(parts[-1]) if len(parts) >= 2 else 0.0
    label_map = {
        'T_BUY': '趋势买入', 'T_ADD': '趋势加仓', 'R_BUY': '震荡买入',
        'BUY': '买入',
        'STOP': '止损', 'TRAIL': '移动止盈',
        'T_SELL': 'MACD转负卖出', 'TIMEOUT': '超时清仓',
        'R_TOP': '上轨清仓', 'R_SELL': '震荡出场',
        'CB_SELL': '央行减持平仓', 'USD_SELL': '美元走强离场',
        'MA_SELL': '跌破MA20出场',
    }
    label = label_map.get(prefix, prefix)
    return label, price


def send_combined_email(email_infos):
    """发送合并的邮件，只包含最后一天有操作的股票"""
    if not email_infos:
        return
    
    # 只包含最后一天有操作的股票
    actions_data = []
    for info in email_infos:
        if info.get('should_send', False):
            actions_data.append({
                'stock_code': info['stock_code'],
                'strategy_name': info.get('strategy_name', '自适应策略'),
                'last_action': info.get('last_action', '无操作'),
                'last_action_quant': info.get('last_action_quant', 0),
                'last_action_date': info.get('last_action_date'),
                'last_action_ratio': info.get('last_action_ratio'),
                'current_position_ratio': info.get('current_position_ratio', 0.0),
                'start_date': info.get('start_date'),
                'end_date': info.get('end_date'),
                'total_return': info['total_return'],
                'annual_return': info['annual_return'],
                'ending_value': info['ending_value'],
                'win_rate': info.get('win_rate', 0),
            })
    
    if not actions_data:
        print("所有股票在最后一天都没有操作，不发送邮件。")
        return
    
    print(f"检测到 {len(actions_data)} 只股票在最后一天有操作，准备发送邮件...")
    
    # 构建邮件正文
    if len(actions_data) == 1:
        # 单只股票
        data = actions_data[0]
        strat_name = data.get('strategy_name', '自适应策略')
        subject = f"{strat_name} - {data['stock_code']}"

        label, price = _parse_action(data['last_action'])
        quant = abs(data.get('last_action_quant', 0))
        amount = quant * price
        date_range = f"{data['start_date']} ~ {data['end_date']}" if data.get('start_date') else ''
        op_ratio = data['last_action_ratio'] if data['last_action_quant'] > 0 else -data['last_action_ratio']
        cur_ratio = data.get('current_position_ratio', 0.0)

        text_body = f"""策略: {strat_name}
股票代码: {data['stock_code']}
操作: {label}
回测区间: {date_range}
收盘价: {price:.2f}
操作数量: {quant}股
当前总资产: {data['ending_value']:.2f}
总收益率: {data['total_return']*100:.2f}%"""

        html_body = f"""
<html>
<body>
<h3>{strat_name} - {data['stock_code']} &nbsp; 胜率{data.get('win_rate', 0):.1f}%</h3>
<table border="1" cellpadding="5" cellspacing="0" style="width:100%;border-collapse:collapse;">
<tr style="background:#f0f0f0;font-weight:bold;">
<td style="padding:6px;">代码</td><td style="padding:6px;">当前仓位</td><td style="padding:6px;">操作仓位</td>
</tr>
<tr>
<td style="padding:6px;">{data['stock_code']}</td>
<td style="padding:6px;">{cur_ratio*100:.1f}%</td>
<td style="padding:6px;">{op_ratio*100:+.1f}%</td>
</tr>
</table>
</body>
</html>"""

        send_email(
            stock_code=data['stock_code'],
            actions=[data['last_action']],
            action_ratios=[data.get('last_action_ratio')],
            send_flag=True,
            subject=subject,
            text_body=text_body,
            html_body=html_body
        )
    else:
        # 多只股票
        strat_name = actions_data[0].get('strategy_name', '自适应策略')
        subject = f"{strat_name}回测完成 - {len(actions_data)}只股票在最后一天有信号产生"
        
        # 构建表格内容
        table_rows = []
        for data in actions_data:
            table_rows.append(f"""
股票代码: {data['stock_code']}
最后操作: {data['last_action']}
交易金额占比: {data.get('last_action_ratio')*100:.2f}%
操作日期: {data['last_action_date']}
总收益率: {data['total_return']*100:.2f}%
年化收益率: {data['annual_return']*100:.2f}%
期末资产: {data['ending_value']:.2f}
{'='*50}""")
        
        text_body = f"""策略: {strat_name}
在最后一天有操作的股票数量: {len(actions_data)}只

详细结果:
{''.join(table_rows)}"""
        
        # 发送合并邮件，使用第一只股票的代码作为主标识
        send_email(
            stock_code=f"多股票回测({len(actions_data)}只有操作)",
            actions=[f"{data['stock_code']}: {data['last_action']} ({data['last_action_date']})" for data in actions_data],
            action_ratios=[data.get('last_action_ratio') for data in actions_data],
            send_flag=True,
            subject=subject,
            text_body=text_body
        )