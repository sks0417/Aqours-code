class ChannelPolicy:
    def __init__(self, disabled_channels=()):
        self.disabled_channels = frozenset(channel.strip().lower()
                                           for channel in disabled_channels)

    def candidates(self, request):
        ordered = []
        for channel in (request.primary_channel, *request.fallback_channels):
            # BUG: disabled channels must never be eligible.
            if channel not in ordered:
                ordered.append(channel)
        return tuple(ordered)
