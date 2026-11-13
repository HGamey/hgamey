#pragma once
#include <cstring>
#include <map>
#include <string>
#include <vector>
#define PROP_VALUE_MAX 92
// Host property store: tests set values and read back which names the layer asked for.
inline std::map<std::string, std::string> rk_host_props;
inline std::vector<std::string> rk_host_prop_reads;
inline int __system_property_get(const char* name, char* value) {
    rk_host_prop_reads.push_back(name);
    auto it = rk_host_props.find(name);
    std::string v = it == rk_host_props.end() ? "" : it->second.substr(0, PROP_VALUE_MAX - 1);
    memcpy(value, v.c_str(), v.size() + 1);
    return (int)v.size();
}
