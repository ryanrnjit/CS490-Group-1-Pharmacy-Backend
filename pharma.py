from flask import Flask, request, Response, jsonify, render_template
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import desc, text
from flask_cors import CORS
from flasgger import Swagger, swag_from
import pika
import requests
import json

HOST = 'localhost'
app = Flask(__name__)
CORS(app, supports_credentials=True, origins=f"http://{HOST}:3000") 
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pharma.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SWAGGER'] = {
    'title': 'BetterU API',
    'description': 'The available endpoints for the BetterU service.',
    'termsOfService': None,
    'doc_dir': './docs/',
    'uiversion': 3,
}

db = SQLAlchemy(app)
swag = Swagger(app)

def listen_for_orders():
    with app.app_context():
        def order_callback(ch, method, properties, body):
            (medication_id, patient_id) = body.decode().split(",")
            print(f"MESSAGE:: Medication ID: {medication_id} Patient ID: {patient_id}")
            try:
                db.session.execute(text("""
                    INSERT INTO orders (order_id, medication_id, status)
                    VALUES (
                        (SELECT MAX(order_id) FROM orders) + 1,
                        :med,
                        'pending'
                    )
                """), {'med': medication_id })
                
            except Exception as e:
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                print(f"SQLITE ERROR:: {e}")
            else:
                db.session.commit()
                ch.basic_ack(delivery_tag=method.delivery_tag)
                print(requests.get(f"http://{HOST}:5000/patients?patient_id={patient_id}").json()) #Just testing rest call
                print("SQLITE:: Added order.")
        connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
        channel = connection.channel()
        channel.queue_declare(queue='orders')
        channel.basic_consume(queue='orders', on_message_callback=order_callback)
        channel.start_consuming()

def send_order_update(params):
    message = json.dumps(params)
    connection = pika.BlockingConnection(pika.ConnectionParameters(HOST))
    channel = connection.channel()
    channel.queue_declare(queue='order_updates')
    channel.basic_publish(exchange='', routing_key='order_updates', body=message)
    print(f"Sent {message}")
    connection.close()

def send_new_medication(params):
    message = json.dumps(params)
    connection = pika.BlockingConnection(pika.ConnectionParameters(HOST))
    channel = connection.channel()
    channel.queue_declare(queue='new_medication')
    channel.basic_publish(exchange='', routing_key='new_medication', body=message)
    print(f"Sent {message}")
    connection.close()

@app.route("/")
def home():
    return "<h1>It Works!</h1>"

@app.route("/inventory", methods=['GET'])
@swag_from('docs/inventory/get.yml')
def get_inventory():
    query = "SELECT * FROM inventory\n"

    params = {
        'inv_id': "" if request.args.get('inventory_id') is None else request.args.get('inventory_id'),
        'med_id': "" if request.args.get('medication_id') is None else request.args.get('medication_id'),
        'stock': "" if request.args.get('stock') is None else request.args.get('stock'),
        'last_updated': "" if request.args.get('last_updated') is None else '%' + request.args.get('last_updated') + '%'
    }
    if params['inv_id'] != "" or params['med_id'] != "" or params['stock'] != "" or params['last_updated'] != "":
        query += (
            "WHERE " +
            ("inventory_id = :inv_id\n" if params['inv_id'] != "" else "TRUE\n") +
            "AND " + ("medication_id = :med_id\n" if params['med_id'] != "" else "TRUE\n") +
            "AND " + ("stock = :stock\n" if params['stock'] != "" else "TRUE\n") +
            "AND " + ("last_updated LIKE :last_updated\n" if params['last_updated'] != "" else "TRUE\n")
        )

    result = db.session.execute(text(query), params)
    json_response = {'inventory': []}
    for row in result:
        json_response['inventory'].append({
            'inventory_id': row.inventory_id,
            'medication_id': row.medication_id,
            'stock': row.stock,
            'last_updated': row.last_updated
        })
    return json_response, 200

@app.route("/medications", methods=['POST'])
@swag_from("docs/medications/post.yml")
def add_medications():
    params = {
        'name': request.json.get('name'),
        'description': request.json.get('description')
    }
    query = text("""
        INSERT INTO medications (medication_id, name, description)
        VALUES (
            (SELECT MAX(medication_id) FROM medications) + 1,
            :name,
            :description
        )
    """)
    #input validation
    if(any(tok in params.values() for tok in (None, ""))):
        return ResponseMessage("Required parameters not sent.", 400)
    try:
        db.session.execute(query, params)
    except Exception as e:
        print(e)
        return ResponseMessage("Server error, please try again later.", 500)
    else:
        db.session.commit()
        send_new_medication(params)
        return ResponseMessage("Medication Added.", 201)

@app.route("/medications", methods=['GET'])
@swag_from('docs/medications/get.yml')
def get_medications():
    # SQL query to fetch medications
    query = "SELECT * FROM medications\n"
    # Gather optional parameters
    params = {
        'med_id': "" if request.args.get('medication_id') is None else request.args.get('medication_id'),
        'name': "" if request.args.get('name') is None else '%' + request.args.get('name') + '%',
        'description': "" if request.args.get('description') is None else '%' + request.args.get('description') + '%'
    }
    if params['med_id'] != "" or params['name'] != "" or params['description'] != "":
        query += (
            "WHERE " +
            ("medication_id = :med_id\n" if params['med_id'] != "" else "TRUE\n") +
            "AND " + ("name LIKE :name\n" if params['name'] != "" else "TRUE\n") +
            "AND " + ("description LIKE :description\n" if params['description'] != "" else "TRUE\n")
        )
    # Execute query and build JSON response
    result = db.session.execute(text(query), params)
    json_response = {'medications': []}
    for row in result:
        json_response['medications'].append({
            'medication_id': row.medication_id,
            'name': row.name,
            'description': row.description
        })
    return json_response, 200

@app.route('/orders/<int:order_id>', methods=['PATCH'])
@swag_from('docs/orders/patch.yml')
def update_order(order_id):
    params = {
        'order_id': order_id,
        'medication_id': request.json.get('medication_id'),
        'status': request.json.get('status'),
        'patient_id': request.json.get('patient_id')
    }
    query = text(f"""
        UPDATE orders SET
            medication_id = {':medication_id' if params['medication_id'] != None else 'medication_id'},
            status = {':status' if params['status'] != None else 'status'},
            patient_id = {':patient_id' if params['patient_id'] != None else 'patient_id'}
        WHERE order_id = :order_id
    """)
    #input validation
    if(all(param == None for param in list(params.values())[1:])):
        return ResponseMessage("Updated nothing.", 200)
    if(not ValidTableID('orders', 'order_id', order_id)):
        return ResponseMessage("Invalid Order ID.", 404)
    if(params['medication_id'] != None and not ValidTableID('medications', 'medication_id', params['medication_id'])):
        return ResponseMessage("Invalid Medication ID.", 400)
    if(params['status'] != None and params['status'].lower() not in ('accepted', 'rejected', 'pending', 'canceled', 'ready')):
        return ResponseMessage("Invalid status. (must be 'accepted', 'rejected', 'pending', 'canceled', or 'ready')", 400)
    #database update
    try:
        db.session.execute(query, params)
    except Exception as e:
        print(e)
        return ResponseMessage("Server error updating order.", 500)
    else:
        db.session.commit()
        send_order_update(params)
        return ResponseMessage("Order Updated.", 200)

@app.route('/patient/<int:patient_id>', methods=['GET'])
@swag_from('docs/patient/get.yml')
def get_patient(patient_id):
    patient =  requests.get(f"http://{HOST}:5000/patients?patient_id={patient_id}").json()['patients']
    user = requests.get(f"http://{HOST}:5000/users?user_id={patient_id}").json()['users']
    print(patient)
    if([] in (patient, user)):
        return ResponseMessage("Invalid patient!", 400)
    patient = patient[0]
    user = user[0]
    return {'patient': {
        'patient_id': patient['patient_id'],
        'first_name': user['first_name'],
        'last_name': user['last_name'],
        'medical_history': patient['medical_history'],
        'ssn': patient['ssn']
    }}, 200
        

@app.route("/orders", methods=['GET'])
@swag_from('docs/orders/get.yml')
def get_orders():
    query = "SELECT O.*, M.name FROM orders AS O JOIN medications AS M ON M.medication_id = O.medication_id\n"
    params = {
        'order_id': "" if request.args.get('order_id') is None else request.args.get('order_id'),
        'medication_id': "" if request.args.get('medication_id') is None else request.args.get('medication_id'),
        'status': "" if request.args.get('status') is None else '%' + request.args.get('status') + '%',
        'patient_id': "" if request.args.get('patient_id') is None else request.args.get('patient_id')
    }
    
    conditions = []
    if params['order_id'] != "":
        conditions.append("O.order_id = :order_id")
    if params['medication_id'] != "":
        conditions.append("O.medication_id = :medication_id")
    if params['status'] != "":
        conditions.append("O.status = :status")
    if params['patient_id'] != "":
        conditions.append("O.patient_id = :patient_id")
    
    if conditions:
        query += "WHERE " + " AND ".join(conditions) + "\n"
    
    result = db.session.execute(text(query), params)
    json_response = {'orders': []}
    for row in result:
        json_response['orders'].append({
            'order_id': row.order_id,
            'medication_id': row.medication_id,
            'name': row.name,
            'status': row.status,
            'patient_id': row.patient_id
        })
    return json_response, 200

def ValidTableID(table:str, id_field:str, id:int):
    return db.session.execute(text(f"SELECT * FROM {table} WHERE {id_field} = :id"), {'id': id}) != None
        
def ResponseMessage(message, code):
    return {'message': message}, code

if __name__ == "__main__":
    import threading
    threading.Thread(target=listen_for_orders, daemon=True).start()
    app.run(debug=True, port=5001)
