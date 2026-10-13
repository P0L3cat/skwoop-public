/* Host check for the wake model pick. No ESP-IDF. */

#include "wake_pick.h"

#include <assert.h>

int main(void)
{
    const char *stock[] = { "mn7_en", "wn9_hiesp", "fst" };
    const char *custom[] = { "wn9_hiesp", "mn7_en", "wn9_heyskwoop" };
    const char *none[] = { "mn7_en", "nsnet2" };
    const char *tricky[] = { "mn_heyskwoop", "wn9_hiesp" };

    assert(wake_pick(stock, 3, "wn", "heyskwoop", "hiesp") == 1);
    assert(wake_pick(custom, 3, "wn", "heyskwoop", "hiesp") == 2);
    assert(wake_pick(custom, 3, "wn", "", "hiesp") == 0);
    assert(wake_pick(none, 2, "wn", "heyskwoop", "hiesp") == -1);
    assert(wake_pick(tricky, 2, "wn", "heyskwoop", "hiesp") == 1);
    assert(wake_pick(stock, 0, "wn", "heyskwoop", "hiesp") == -1);
    assert(wake_pick(NULL, 3, "wn", "heyskwoop", "hiesp") == -1);

    return 0;
}
