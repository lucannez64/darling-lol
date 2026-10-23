cmake_minimum_required(VERSION 3.20)
project(darling_lol_splash_demo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Qt6 REQUIRED COMPONENTS Widgets Svg)

add_executable(darling-splash-demo main.cpp)
target_link_libraries(darling-splash-demo PRIVATE Qt6::Widgets Qt6::Svg)

file(COPY assets DESTINATION "${CMAKE_CURRENT_BINARY_DIR}")
