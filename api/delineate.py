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

        headers = {'User-Agent': 'Mozilla/5.0'}

        # Watershed boundary polygon
        wshed_url = f"https://mghydro.com/app/watershed_api?lat={lat}&lng={lng}&precision=high"
        req = urllib.request.Request(wshed_url, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as response:
            watershed_data = json.loads(response.read().decode('utf-8'))

        # Upstream river network (optional - don't fail the whole request if this part breaks)
        rivers_data = None
        try:
            rivers_url = f"https://mghydro.com/app/upstream_rivers_api?lat={lat}&lng={lng}"
            req2 = urllib.request.Request(rivers_url, headers=headers)
            with urllib.request.urlopen(req2, timeout=25) as response2:
                rivers_data = json.loads(response2.read().decode('utf-8'))
        except Exception:
            rivers_data = None

        props = {}
        if watershed_data.get('features'):
            props = watershed_data['features'][0].get('properties', {})

        return jsonify({
            'watershed': watershed_data,
            'rivers': rivers_data,
            'outlets': {'lat': lat, 'lng': lng},
            'morphology': props
        }), 200
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500
