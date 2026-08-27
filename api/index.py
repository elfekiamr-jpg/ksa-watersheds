from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

app = Flask(__name__)
CORS(app)

@app.route('/api/delineate', methods=['POST'])
@app.route('/delineate', methods=['POST'])
def delineate():
    try:
        data = request.get_json() or {}
        lat = data.get('lat')
        lng = data.get('lng')

        if lat is None or lng is None:
            return jsonify({'error': 'Latitude and longitude are required.'}), 400

        # Proxy call to mghydro delineation engine
        external_url = f"https://mghydro.com/api/delineate?lat={lat}&lng={lng}"
        response = requests.get(external_url, timeout=9)
        
        if not response.ok:
            return jsonify({'error': 'Delineation service unavailable.'}), 502

        res_data = response.json()
        return jsonify({
            'watershed': res_data.get('watershed'),
            'rivers': res_data.get('rivers'),
            'outlets': res_data.get('outlets'),
            'morphology': res_data.get('morphology', {})
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'status': 'API Running'}), 200
