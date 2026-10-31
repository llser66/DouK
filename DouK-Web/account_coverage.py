"""Conservative completeness evidence from the existing Account acquisition flow."""


class Coverage:
    def __init__(self):
        self.valid = True
        self.pages = 0
        self.last_more = None
        self.boundary = False
        self.cursors = set()
        self.raw_count = 0
        self.oldest_published = None
        self.newest_published = None

    def observe(self, data, data_key, cursor, has_more):
        if not isinstance(data, dict) or data.get('status_code', 0) != 0 or not isinstance(data.get(data_key), list) or data.get(has_more) not in (True, False, 0, 1) or cursor not in data:
            self.valid = False
            return
        self.pages += 1
        self.raw_count += len(data[data_key])
        for row in data[data_key]:
            published = row.get('create_time') if isinstance(row, dict) else None
            if isinstance(published, (int, float)) and published > 0:
                self.oldest_published = min(self.oldest_published or published, published)
                self.newest_published = max(self.newest_published or published, published)
        more = bool(data[has_more])
        position = str(data[cursor])
        if more and (not data[data_key] or position in self.cursors):
            self.valid = False
        self.cursors.add(position)
        self.last_more = more

    def result(self):
        return {'complete':bool(self.valid and self.pages and (self.last_more is False or self.boundary)),
                'pages':self.pages,'boundary_reached':self.boundary,
                'raw_count':self.raw_count,'oldest_published':self.oldest_published,
                'newest_published':self.newest_published}
