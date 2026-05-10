from flask import Flask, request, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import xml.etree.ElementTree as ET
import requests
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

db_url = os.environ.get('DATABASE_URL', '')
app.config['SQLALCHEMY_DATABASE_URI'] = db_url.replace('mysql://', 'mysql+pymysql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
    'pool_timeout': 20,
    'pool_size': 5,
    'max_overflow': 2,
}

db = SQLAlchemy(app)

# These will be set as environment variables on Railway
INVENTORY_URL = os.environ.get('INVENTORY_URL', 'http://127.0.0.1:5001') + '/update_inventory'
PAYMENT_URL   = os.environ.get('PAYMENT_URL',   'http://127.0.0.1:5002') + '/process_payment'

class ProductPrice(db.Model):
    __tablename__ = 'product_prices'
    id    = db.Column(db.Integer, primary_key=True)
    name  = db.Column(db.String(100), unique=True, nullable=False)
    price = db.Column(db.Float, nullable=False)

class Order(db.Model):
    __tablename__ = 'orders'
    id           = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(100), nullable=False)
    quantity     = db.Column(db.Integer, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    status       = db.Column(db.String(50), nullable=False, default='Pending')
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

@app.before_request
def setup():
    db.create_all()
    if ProductPrice.query.count() == 0:
        seeds = [
            ProductPrice(name='Cobra',    price=20),
            ProductPrice(name='Sting',    price=20),
            ProductPrice(name='Red Bull', price=50),
            ProductPrice(name='Monster',  price=65),
            ProductPrice(name='Predator', price=20),
        ]
        db.session.add_all(seeds)
        db.session.commit()

@app.route('/get_products', methods=['GET'])
def get_products():
    try:
        products = ProductPrice.query.all()
        response = ET.Element('Products')
        for p in products:
            prod = ET.SubElement(response, 'Product')
            ET.SubElement(prod, 'Name').text  = p.name
            ET.SubElement(prod, 'Price').text = str(p.price)
        return Response(ET.tostring(response), mimetype='application/xml')
    except Exception as e:
        response = ET.Element('Products')
        ET.SubElement(response, 'Error').text = str(e)
        return Response(ET.tostring(response), mimetype='application/xml')

@app.route('/get_orders', methods=['GET'])
def get_orders():
    try:
        orders   = Order.query.order_by(Order.created_at.desc()).all()
        response = ET.Element('Orders')
        for o in orders:
            order_el = ET.SubElement(response, 'Order')
            ET.SubElement(order_el, 'ID').text          = str(o.id)
            ET.SubElement(order_el, 'ProductName').text = o.product_name
            ET.SubElement(order_el, 'Quantity').text    = str(o.quantity)
            ET.SubElement(order_el, 'TotalAmount').text = str(o.total_amount)
            ET.SubElement(order_el, 'Status').text      = o.status
            ET.SubElement(order_el, 'CreatedAt').text   = str(o.created_at)
        return Response(ET.tostring(response), mimetype='application/xml')
    except Exception as e:
        response = ET.Element('Orders')
        ET.SubElement(response, 'Error').text = str(e)
        return Response(ET.tostring(response), mimetype='application/xml')

def safe_parse(content):
    if isinstance(content, bytes):
        content = content.decode('utf-8', errors='ignore')
    content = content.strip()
    if content.startswith('<?xml'):
        content = content[content.index('?>') + 2:].strip()
    if content.startswith('<html') or content.startswith('<!DOCTYPE'):
        raise Exception('Service returned an error page. Please try again.')
    return ET.fromstring(content)

@app.route('/place_order', methods=['POST'])
def place_order():
    try:
        root         = safe_parse(request.data)
        product_name = root.find('ProductName').text
        quantity     = int(root.find('Quantity').text)

        product = ProductPrice.query.filter_by(name=product_name).first()
        if not product:
            raise Exception("Invalid product")

        total_amount = product.price * quantity

        new_order = Order(
            product_name=product_name,
            quantity=quantity,
            total_amount=total_amount,
            status='Pending'
        )
        db.session.add(new_order)
        db.session.commit()

        inventory_response = requests.post(
            INVENTORY_URL,
            data=request.data,
            headers={'Content-Type': 'application/xml'}
        )
        inv_root   = safe_parse(inventory_response.content)
        inv_status = inv_root.find('Status')

        if inv_status is None or inv_status.text != 'Success':
            new_order.status = 'Failed - Inventory'
            db.session.commit()
            return Response(inventory_response.content, mimetype='application/xml')

        payment_xml = ET.Element('Payment')
        ET.SubElement(payment_xml, 'Amount').text = str(total_amount)

        payment_response = requests.post(
            PAYMENT_URL,
            data=ET.tostring(payment_xml),
            headers={'Content-Type': 'application/xml'}
        )
        pay_root   = safe_parse(payment_response.content)
        pay_status = pay_root.find('Status')

        if pay_status is None or pay_status.text != 'Success':
            new_order.status = 'Failed - Payment'
            db.session.commit()
            return Response(payment_response.content, mimetype='application/xml')

        new_order.status = 'Completed'
        db.session.commit()

        confirmation = ET.Element('OrderConfirmation')
        ET.SubElement(confirmation, 'Status').text      = 'Success'
        ET.SubElement(confirmation, 'OrderID').text     = str(new_order.id)
        ET.SubElement(confirmation, 'ProductName').text = product_name
        ET.SubElement(confirmation, 'Quantity').text    = str(quantity)
        ET.SubElement(confirmation, 'AmountPaid').text  = str(total_amount)
        ET.SubElement(confirmation, 'Message').text     = 'Order confirmed and saved'
        return Response(ET.tostring(confirmation), mimetype='application/xml')

    except Exception as e:
        error_xml = ET.Element('OrderConfirmation')
        ET.SubElement(error_xml, 'Status').text  = 'Error'
        ET.SubElement(error_xml, 'Message').text = str(e)
        return Response(ET.tostring(error_xml), mimetype='application/xml')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)