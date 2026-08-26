from app import create_app
import yaml, os

app = create_app()

if __name__ == '__main__':
    settings_path = os.path.join(os.path.dirname(__file__), 'config', 'settings.yaml')
    with open(settings_path) as f:
        settings = yaml.safe_load(f)

    server = settings.get('server', {})
    app.run(
        host=server.get('host', '0.0.0.0'),
        port=server.get('port', 5000),
        debug=server.get('debug', False),
    )
