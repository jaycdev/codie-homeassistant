# Changelog

## [0.2.0](https://github.com/jaycdev/codie-homeassistant/compare/v0.1.0...v0.2.0) (2026-11-24)


### Features

* let Home Assistant adjust the Codie's volume, screen and side lights ([#6](https://github.com/jaycdev/codie-homeassistant/issues/6)) ([65e04ce](https://github.com/jaycdev/codie-homeassistant/commit/65e04ce5e9e0b3f083e26caeda71d41eec6e53e0))


### Bug Fixes

* offer codie.announce only for Codie targets in the action picker ([#4](https://github.com/jaycdev/codie-homeassistant/issues/4)) ([3ebd4c2](https://github.com/jaycdev/codie-homeassistant/commit/3ebd4c276ffd828cc90350fc8c1a2ef6baf1dc42))

## 0.1.0 (2026-11-22)


### ⚠ BREAKING CHANGES

* tool parameter schemas render differently, so every Tool whose definition changed gets a new version on the next catalog push.
* Home Assistant older than 2026.11.2 is no longer supported. The tests no longer run on Windows.

### Features

* keep the executor connected with backoff, and refresh the catalog when Assist tools change ([5199fd7](https://github.com/jaycdev/codie-homeassistant/commit/5199fd75efd283d8b9165c353a7d4f9e664870e0))
* let automations make the Character speak with a notify entity and codie.announce ([a41f9e6](https://github.com/jaycdev/codie-homeassistant/commit/a41f9e68dad1643e8d1cb273cf50bb8cc89bd189))
* log what a failure report needs: request status, turn_id and request_id ([3086423](https://github.com/jaycdev/codie-homeassistant/commit/3086423563c54195b2e8632541bdcd431fb89b8a))
* offer the full fixed Tool list with schema conversion ([367610c](https://github.com/jaycdev/codie-homeassistant/commit/367610c21c30956426aa61c591d94762d35ea96b))
* pair a Codie and add it as a device ([21a4fd8](https://github.com/jaycdev/codie-homeassistant/commit/21a4fd84401a2dfc01f88d822eb12ffb3d108d30))
* render tool schemas with probatio's OpenAPI 3.1 encoder ([cc7c56d](https://github.com/jaycdev/codie-homeassistant/commit/cc7c56de95e519ef369de28b5a9706194ea55211))
* require Home Assistant 2026.11.2 or later ([e378bb3](https://github.com/jaycdev/codie-homeassistant/commit/e378bb322ac219754d8926eab5e65c25f9d2c239))
* send queued announcements with codie.announce queue: true ([b883483](https://github.com/jaycdev/codie-homeassistant/commit/b88348320619b9445388a498dfa7e97006cce8cf))
* serve get_home_state to the Codie over the executor WebSocket ([1f7f836](https://github.com/jaycdev/codie-homeassistant/commit/1f7f836b60419c7256af6073419866a93988002b))
* start re-authentication when the Codie rejects the stored credentials ([eafac1b](https://github.com/jaycdev/codie-homeassistant/commit/eafac1bb7435a964a14b1d903bc92034c700e427))


### Bug Fixes

* check the clock before treating INVALID_SIGNATURE as rejected credentials ([1b15369](https://github.com/jaycdev/codie-homeassistant/commit/1b15369535e9b4caaf635e42699f0aae24863b24))
* give an untyped parameter a string type so the Codie accepts the catalog ([a673543](https://github.com/jaycdev/codie-homeassistant/commit/a673543eed4130b6c2721ac7393b58fd90604ba2))
* keep only the enum values that fit their parameter's type ([4d3e6a4](https://github.com/jaycdev/codie-homeassistant/commit/4d3e6a4bf6a3dc3e024cc77566d05623d64f67f6))
* make Assist's duplicate and missed names answerable ([ee9e55f](https://github.com/jaycdev/codie-homeassistant/commit/ee9e55fa8b37ac4d3dce56e1a226c916f8a83fb9))
* run Tool calls in the Codie's AI language ([6d87c33](https://github.com/jaycdev/codie-homeassistant/commit/6d87c33e5cc6200e9f4d73f016ad20f1f3f26d28))
* say no to-do list is exposed when todo_get_items is unavailable ([bf44c36](https://github.com/jaycdev/codie-homeassistant/commit/bf44c3693e5d6b11bce9adcdf64716a428e96cc1))
* start re-authentication when an Announcement is rejected for its credentials ([3a02eec](https://github.com/jaycdev/codie-homeassistant/commit/3a02eec00669417b4b8d3d5a0b323e2c5d2eb4b3))
* tell a call without a Codie target apart from a Codie that is not loaded ([d6c45da](https://github.com/jaycdev/codie-homeassistant/commit/d6c45da2be378949b26ddbcd166b12cacfc920dd))
