/* Host check for the broker URL parse. */

#include "mqtt_url.h"

#include <assert.h>
#include <string.h>

int main(void)
{
    mqtt_url_t u;

    assert(mqtt_url_parse("mqtt://broker.example", &u) == 0 && !u.tls && u.port == 1883 &&
           strcmp(u.host, "broker.example") == 0);
    assert(mqtt_url_parse("mqtts://broker.example.com:8884", &u) == 0 && u.tls && u.port == 8884);
    assert(mqtt_url_parse("mqtts://b", &u) == 0 && u.port == 8883);
    assert(mqtt_url_parse("http://b", &u) == -1);
    assert(mqtt_url_parse("mqtt://", &u) == -1);
    assert(mqtt_url_parse("mqtt://b:", &u) == -1);
    assert(mqtt_url_parse("mqtt://b:0", &u) == -1);
    assert(mqtt_url_parse("mqtt://b:65536", &u) == -1);
    assert(mqtt_url_parse("mqtt://b:12a", &u) == -1);
    assert(mqtt_url_parse("mqtt://user@b", &u) == -1);
    assert(mqtt_url_parse("mqtt://b/path", &u) == -1);
    assert(mqtt_url_parse(NULL, &u) == -1);

    return 0;
}
