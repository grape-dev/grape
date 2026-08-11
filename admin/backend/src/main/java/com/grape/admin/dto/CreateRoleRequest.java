package com.grape.admin.dto;

import lombok.Getter;
import lombok.Setter;

import javax.validation.constraints.NotBlank;
import javax.validation.constraints.Size;

@Getter
@Setter
public class CreateRoleRequest {

    @NotBlank
    @Size(min = 2, max = 64)
    private String code;

    @NotBlank
    @Size(max = 64)
    private String name;
}
