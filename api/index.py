from flask import Flask, request, jsonify
from flask_cors import CORS
import urllib.request
import json

app = Flask(__name__)
CORS(app)

@app.route('/api/delineate', methods=['POST', 'GET'])
@app.route('/delineate', methods=['POST', 'GET'])
def delineate():
    if request.method == 'GET':
        return jsonify({'status': 'API endpoint active. Send POST request with lat/lng.'}), 200

    try:
        data = request.get_json(force=True, silent=True) or {}
        lat = data.get('lat')
        lng = data.get('lng')

        if lat is None or lng is None:
            return jsonify({'error': 'Latitude and longitude parameters are required.'}), 400

        # Query external delineation engine directly
        external_url = f"https://mghydro.com/api/delineate?lat={lat}&lng={lng}"
        req = urllib.request.Request(external_url, headers={'User-Agent': 'Mozilla/5.0'})
        
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode('utf-8'))

        return jsonify({
            'watershed': res_data.get('watershed'),
            'rivers': res_data.get('rivers'),
            'outlets': res_data.get('outlets'),
            'morphology': res_data.get('morphology', {})
        }), 200

    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

# Fallback route
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    return jsonify({'message': 'KSA Watersheds API active'}), 200
