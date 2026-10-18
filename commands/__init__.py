from .lighten import entry as lighten
from .pulley import entry as pulley
from .belt import entry as belt
from .bridging import entry as bridging
from .sketch_tools import center as center_distance
from .sketch_tools import diameter as pulley_diameter

# Every tool module exposes start() and stop(). Add new tools here.
COMMANDS = [lighten, pulley, belt, bridging, pulley_diameter, center_distance]
