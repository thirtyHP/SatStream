def classFactory(iface):
    """Load the SatStream plugin.

    :param iface: QgisInterface instance.
    """
    from .plugin import SatStreamPlugin
    return SatStreamPlugin(iface)
