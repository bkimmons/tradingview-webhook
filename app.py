from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os, json

app = Flask(__name__)

api = tradeapi.REST(
    os.environ['ALPACA_API_KEY'],
    os.environ['ALPACA_SECRET_KEY'],
    base_url=os.environ['ALPACA_BASE_URL']
)

@app.route('/')
def home():
    return 'Webhook server running!', 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = json.loads(request.data)
    account = api.get_account()
    equity = float(account.equity)
    risk = equity * (data['risk_percentage'] / 100)
    qty = max(1, int(risk / float(data['entry_price'])))
    if data['action'] in ('buy', 'sell'):
        api.submit_order(
            symbol=data['symbol'],
            qty=qty,
            side=data['action'],
            type=data.get('order_type', 'market'),
            time_in_force=data.get('time_in_force', 'day')
        )
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
