"""cPanel/Passenger entry point."""

from aistat.wsgi import create_app

application = create_app()
