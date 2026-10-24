# Attribution

NetAdmin stands on the shoulders of these projects. All trademarks remain the
property of their respective owners; links are provided for identification.

## Python stack (application dependencies)

| Component                                            | Used for                          | License      |
| ---------------------------------------------------- | --------------------------------- | ------------ |
| [Flask](https://flask.palletsprojects.com/)          | Web framework                     | BSD-3-Clause |
| [Flask-WTF](https://flask-wtf.readthedocs.io/)       | Forms and CSRF protection         | BSD-3-Clause |
| [WTForms](https://wtforms.readthedocs.io/)           | Form handling                     | BSD-3-Clause |
| [dnspython](https://www.dnspython.org/)              | AXFR and RFC 2136 dynamic updates | ISC          |
| [gunicorn](https://gunicorn.org/)                    | WSGI HTTP server                  | MIT          |
| [httpx](https://www.python-httpx.org/)               | HTTP client for agent calls       | BSD-3-Clause |
| [argon2-cffi](https://github.com/hynek/argon2-cffi/) | Argon2id password hashing         | MIT          |

## Service daemons

| Component                              | Used for                     | License |
| -------------------------------------- | ---------------------------- | ------- |
| [Bind 9](https://www.isc.org/bind/)    | DNS server                   | MPL-2.0 |
| [ISC DHCPD](https://www.isc.org/dhcp/) | DHCP server (legacy backend) | MPL-2.0 |
| [Kea DHCP](https://www.isc.org/kea/)   | DHCP server (Kea backend)    | MPL-2.0 |
| [FreeRADIUS](https://freeradius.org/)  | RADIUS server                | GPL-2.0 |

## Frontend and documentation tooling

| Component                                                           | Used for              | License |
| ------------------------------------------------------------------- | --------------------- | ------- |
| [Bootstrap](https://getbootstrap.com/)                              | UI framework          | MIT     |
| [MkDocs](https://www.mkdocs.org/)                                   | Static site generator | BSD     |
| [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) | Documentation theme   | MIT     |

Container base images are Ubuntu; see each published image's manifest for the
exact package set and licenses. NetAdmin itself is distributed under the
license in [LICENSE](https://github.com/timlaing/net-admin/blob/main/LICENSE).
